# SCチャットエージェント 仕様書

## 1. ツール概要

**名称：** SCチャットエージェント
**目的：** 助成金申請に必要な書類の作成を、AIとの対話を通じてサポートするWebアプリ
**対象ユーザー：** 社内担当者（社内利用限定）
**公開方法：** Streamlit CloudでURL配布（社内周知・ログイン必須）

> ⚠️ 本システムは「書類作成AIエージェント」（顧客向け）の社内版として複製したもの。
> Supabase・Gemini APIキーは顧客向けシステムとは**別系統**で運用する（データとレート制限の分離）。

### ボットのペルソナ
- 「○○専門の助成金申請サポートAI」として動作
- **社労士などの専門家としての法的責任は負わない**旨を明示（AIによるサポート情報という立場）
- Gemini 2.5 Flashの思考プロセスはユーザーに表示しない

---

## 2. 技術スタック

| 項目 | 内容 |
|------|------|
| フロントエンド/バックエンド | Streamlit |
| AIモデル | Gemini 2.5 Flash（google-genai SDK） |
| 言語 | Python |
| デプロイ | Streamlit Cloud（GitHub連携・自動デプロイ） |
| リポジトリ | GitHub: `regalcs1027/sc-chat-agent-r7`（R7版） / `regalcs1027/sc-chat-agent-r8`（R8版） |

### ⚠️ 重要：Streamlitバージョン固定
```
streamlit==1.43.0
```
**Streamlit 1.55.0にはモバイルブラウザのWebSocket接続バグがあり、スマホで開けなくなる。必ずバージョンを固定すること。**

### requirements.txt
```
streamlit==1.43.0
google-genai
python-dotenv
python-docx
openpyxl
pymupdf
```

### 環境変数 / Secrets
- `GEMINI_API_KEY`：Gemini APIキー
  - ローカル：`.env` ファイル
  - Streamlit Cloud：Settings → Secrets に設定

---

## 3. ファイル構成

```
SCチャットエージェント/
├── app.py                    # メインアプリ（唯一のエントリポイント）
├── admin.py                  # 管理画面UI
├── auth.py                   # ログイン・権限
├── db.py                     # PostgreSQL CRUD
├── rulings.py                # 管理者による修正事例の類似検索
├── requirements.txt
├── .env                      # ローカル用（gitignore済み）
└── domains/                  # ドメイン（制度）ごとのデータフォルダ
    ├── キャリアアップ/
    │   ├── domain_config.json      # 制度設定・form_to_stageマッピング
    │   ├── form_structures.json    # 様式ごとの項目定義
    │   ├── basic_rules.json        # ルール・数値定義・事例（applies_to付き）
    │   ├── pdf_chunks.json         # PDFをチャンク分割したRAGデータ
    │   ├── templates/              # 様式PDF（プレビュー用）
    │   └── knowledge/              # 元となるPDF群（ビルドスクリプト用）
    └── 雇用管理制度/
        ├── domain_config.json
        ├── form_structures.json
        ├── basic_rules.json
        ├── pdf_chunks.json
        ├── templates/
        └── knowledge/
```

### 新しいドメインの追加方法
`domains/` 以下に同じ構造のフォルダを作るだけで、アプリが自動検出する（`scan_domains()` 関数）。

---

## 4. 各JSONファイルの構造

### domain_config.json
```json
{
  "display_name": "人材確保等支援助成金（雇用管理制度・...）",
  "applies_to_options": ["計画届", "支給申請", "全般"],
  "form_to_stage": {
    "様式第a-1号_〇〇.pdf": "計画届",
    "様式第a-6号_〇〇.pdf": "支給申請"
  }
}
```

### form_structures.json
```json
{
  "様式名.pdf": {
    "items": [
      {
        "item_id": "①",
        "label": "項目名",
        "type": "text | number | date | select | checkbox",
        "instruction": "記載方法の説明"
      }
    ]
  }
}
```

### basic_rules.json
```json
[
  {
    "rule_id": "rule_001",
    "category": "支給額",
    "content": "ルールの内容",
    "applies_to": ["計画届", "全般"]  // ← 省略可（省略時は全件適用）
  }
]
```

### pdf_chunks.json
```json
[
  {
    "source": "ファイル名.pdf",
    "content": "PDFから抽出したテキスト（チャンク単位）"
  }
]
```

---

## 5. アプリの画面フロー

```
[セットアップ画面]
  ↓ 制度を選択（selectbox）
  ↓ 様式を選択（selectbox）または「全般（様式を特定しない）」
  ↓「相談を開始する」ボタン
[チャット画面]
  ├── 左サイドバー：添削モード / 最初の画面に戻る / 様式プレビュー
  ├── メインエリア：チャット履歴 + テキスト入力
  └── 右カラム：様式の項目一覧ボタン（様式選択時のみ）
```

---

## 6. 主要機能の詳細

### 6-1. チャット（send_and_stream）
- Gemini 2.5 Flashでストリーミング応答
- フォールバック：2.5-flash → 2.0-flash（レート制限時）
- 思考チャンク（`part.thought == True`）は表示しない
- `content` や `content.parts` が `None` のチャンクはスキップ（NoneType対策）

### 6-2. applies_toフィルタリング
選択した様式のステージ（計画届/支給申請）に基づき、`basic_rules.json` のルールを絞り込む。
- `get_stage_for_form(selected_form, domain_config)` → ステージを取得
- `filter_rules_by_stage(rules, stage)` → 該当ルールのみ抽出
- ステージ不明・全般選択時は全ルールを使用
- **ハードコーディングなし：** マッピングは `domain_config.json` の `form_to_stage` に記述

### 6-3. RAG（バイグラム検索）
`get_relevant_chunks(query, pdf_chunks, max_chunks=3)` で関連チャンクを取得し、システムプロンプトに注入。
- 日本語対応：バイグラム（2文字）一致スコアで上位3件を選択

### 6-4. 回答タイプの判別（5タイプ）
システムプロンプトで以下5タイプを判別して回答スタイルを切り替える：
1. チェック型 → ルールのみ、事例引用禁止
2. 自由記述型 → 参考事例を引用して記入見本を作成
3. 数値・計算型 → 計算式明示＋ヒアリング後に計算
4. 日付・期間型 → 期限警告を最優先
5. 選択・フラグ型 → 定義の違いを解説して選択基準を提示

### 6-5. 添削モード
PDF / Word(.docx) / Excel(.xlsx) をアップロードして添削。
- applies_toフィルタリングも適用

### 6-6. 様式プレビュー
サイドバーから選択中の様式PDFを画像として表示（PyMuPDF使用、dpi=150）。

### 6-7. 質問一覧と管理者確認（管理画面「📋 質問一覧」）

「会話履歴閲覧」はユーザー→会話の2段階で人ごとにしか見られないため、
全ユーザー横断で質問と回答を1行にした一覧を別に用意している。

- 質問と回答の対応付けは `messages` を `LAG` で1つ前と突き合わせ、
  直前が `role='user'` の `role='assistant'` 行だけを採用する（`_qa_base_query()`）
- 絞り込み: 未確認のみ / 年度 / キーワード / 期間
- 行を選ぶと全文が出て「✅ 確認済みにする」「✏️ この回答を修正」ができる
- 絞り込み結果をそのまま CSV でダウンロードできる

**確認状態は `messages` の列**（`reviewed` / `reviewed_by` / `reviewed_at`）。
AI回答のメッセージ側に付ける。`notified_at` は未確認通知メールを送った時刻で、
同じ質問を何度も通知しないための印（メール機能は未実装）。

⚠️ CSVは **BOM付きUTF-8**（`utf-8-sig`）で出力すること。BOMが無いと Excel で
ダブルクリックしたときに日本語が化ける。回答は改行・カンマ・引用符を含むため
`QUOTE_ALL` で出力している。

### 6-8. メール通知（GitHub Actions）

`tools/notify.py` を **R8リポジトリの GitHub Actions が毎時実行**する。
Streamlit Cloud のアプリは誰も使っていないとプロセスが止まるため、
アプリ内のタイマーでは通知を取りこぼす。そのため外部から回している。
DBは R7/R8 で共通なので、**ワークフローは R8 側の1本だけ**で両年度を処理する。

| 通知 | 宛先 | タイミング |
|---|---|---|
| 未確認の質問 | `MAIL_TO_QUESTION` | 投稿から1時間経過後。年度ごとに1通 |
| 回答の修正 | `MAIL_TO_REVISION` | 修正1件につき1通（最大1時間の遅れ） |

**二重送信しない仕組み**: 通知した対象に `notified_at` の印を付ける
（質問は `messages`、修正は `admin_rulings`）。一度通知した質問は、
未確認のまま残っても再通知しない。

**公開リポジトリでSecretsを扱うための制約**:
起動条件を `schedule` と `workflow_dispatch` だけに限定すること。
`pull_request` 系のトリガーを足すと、外部からのPRでSecretsが漏れる経路になる。

**送信時の安全策**（notify.py）:
- 宛先は環境変数で固定。外部入力から宛先が決まる経路を作らない
- 件名・宛先に外部由来の文字列を入れない（ヘッダーインジェクション対策）。
  質問文などは必ず本文だけに入れる
- 1回の実行で `MAX_MAILS`（10通）まで。不具合による大量送信の歯止め
- 送信専用。メールの読み取りは行わない

**必要な GitHub Secrets**: `DATABASE_URL` / `SMTP_USER` / `SMTP_PASS` /
`MAIL_TO_QUESTION` / `MAIL_TO_REVISION`
（`DATABASE_URL` は **6543** の Transaction pooler を使うこと）

### 6-9. domain_config.json の important_notes（制度単位の最重要前提）

`domain_config.json` に `important_notes`（文字列の配列）を書くと、
システムプロンプトの**上位**（本日の日付の直後）に専用ブロックとして差し込まれる。
チャットにも添削モードにも適用される。未設定の制度では何も出力されない。

**用途**: 「公式資料に書かれていない前提」を伝えるための枠。

典型例が**廃止されたコース**。廃止コースの資料をナレッジから除外すると、
「廃止された」と書かれた資料まで消えるため、AIは現行制度として案内してしまう。
実際に特定求職者雇用開発助成金で発生した（就職氷河期世代安定雇用実現コースについて、
後継である中高年層安定雇用支援コースの要件を旧コースの要件として断定回答した）。

`basic_rules.json` に登録するだけでは、他の1,700件以上のルールに埋もれて読まれない。
そのため両方に入れている（`important_notes` が主、`basic_rules` は保険）。

```json
{
  "display_name": "特定求職者雇用開発助成金",
  "important_notes": [
    "「就職氷河期世代安定雇用実現コース」は令和7年3月31日をもって廃止されており…"
  ]
}
```

### 6-10. 管理者による修正事例（裁定）

AIの回答が実務的に誤っていた場合に、管理者が正しい回答を登録しておき、
以後似た質問が出たときに現場の画面へ参考表示する仕組み。社内版のみの機能。

**Phase 1（現行）＝表示のみ**

修正事例はシステムプロンプトに渡さない。AIは従来どおり回答を生成し、
その下に別枠で事例を表示する。誤爆（無関係な事例のヒット）が起きても
AIの回答本文が汚染されないようにするため。

将来 Phase 2（プロンプト注入）を検討する場合は、`ruling_hits` に溜まった
スコア分布を確認し、しきい値を決めてから判断する。
`admin_rulings.use_in_prompt` はそのための予約カラム（現在は未使用）。

**登録の流れ**

1. 管理画面 →「💬 会話履歴閲覧」→ 対象のAI回答の下の「✏️ この回答を修正」
2. 質問文（検索キー）・正しい回答・適用範囲・メモを入力して登録
   - 質問文は編集可能。特定の会社の事情に依存した表現は一般化して登録する
   - 登録前に似た既存事例を提示して重複登録を防ぐ

**適用範囲の3段階**

| 適用範囲 | 表示条件 | 使いどころ |
|---|---|---|
| この制度・様式のみ | 制度が一致 **かつ様式も一致** | 記入欄など様式固有の裁定 |
| この制度全体（既定） | 制度が一致すればどの様式でも | 通常はこれ |
| 全体共通 | どの制度でも（判定は厳しくなる） | 原則使わない |

⚠️ 元の会話で様式を選んでいない場合、`form_name` には `全般（様式を特定しない）` が入る。
これを様式として絞ると「様式を選んでいない利用者にしか表示されない」事例になるため、
登録フォームでは「様式のみ」の選択肢を出さず、`_in_scope()` でも制度全体として扱う。
3. 一覧・編集・有効化/無効化は「⚖️ 修正事例」から

**類似判定（rulings.py）**

| 方式 | 用途 |
|---|---|
| 埋め込みベクトル（`gemini-embedding-001` / 768次元） | 本命。言い換えを拾える |
| バイグラム一致（Dice係数） | フォールバック。APIキーが埋め込みモデル非対応の場合 |

ベクトルは登録時に1回だけ計算し `admin_rulings.embedding` に JSON で保存する。
質問時は質問文のみベクトル化し、Python側で内積を取る（pgvector不要）。
どちらの方式で動いているかは、管理画面「⚖️ 修正事例」→「🔧 類似判定の方式を確認」で判定できる。

**⚠️ 再デプロイでは反映されない変更が2種類ある**

Streamlit Cloud は再デプロイ時に**プロセスを作り直さず、スクリプトを再実行するだけ**。
そのため次の2つは、pushしただけでは反映されない。症状も対処も別物なので混同しないこと。

| 変更内容 | 症状 | 対処 |
|---|---|---|
| `db.py` / `rulings.py` などに**関数を追加**した | `ImportError: cannot import name ...` | **⋮ → Reboot app が必須** |
| **テーブル定義だけ**を変えた | `psycopg2.errors.UndefinedTable` | `SCHEMA_VERSION` を +1 |

**関数の追加**: Python は一度読み込んだモジュールを `sys.modules` にキャッシュする。
スクリプトを再実行しても `from db import 新関数` は古いモジュールを見るため失敗する。
`SCHEMA_VERSION` では回避できない。Reboot でプロセスを作り直すしかない。
（修正事例の機能を R8・R7 に入れたとき、両方で実際に踏んだ）

**テーブル定義**: `create_tables()` は `@st.cache_resource` でプロセスに1回しか走らない。
`app.py` の `SCHEMA_VERSION` を +1 すればキャッシュキーが変わり、初期化が再実行される。

→ **両方を同時に変えたときは、結局 Reboot が必要**。迷ったら Reboot すれば確実。

**しきい値の調整（適用範囲で2段階）**

日本語の質問文は「〜する必要がありますか」のような文体が共通するだけでスコアが上がるため、
しきい値が低いと無関係な質問にも事例が出る（実際に 0.72 で誤爆した）。

⚠️ **スコアは質問文だけで決まる。制度・様式はスコアに影響しない**（絞り込みフィルタでしかない）。
そのため「全体共通」で登録した事例は、どの制度の質問にも届いてしまう。
これを踏まえ、しきい値は適用範囲で2段階に分けている。

| 適用範囲 | 既定値 | 理由 |
|---|---|---|
| 制度（・様式）を指定 | 0.85 | 影響範囲が制度内に限られる |
| 全体共通 | 0.95 | どの制度にも出るので、ほぼ同じ言い回しのときだけ |

実測値（登録した質問文＝「キャリアアップ計画書はいつまでに提出する必要がありますか？」）:

| 質問 | スコア | 制度指定(0.85) | 全体共通(0.95) |
|---|---|---|---|
| キャリアアップ計画書の提出期限を教えてください | 0.979 | 表示 | 表示 |
| 計画書はいつまでに出せばいいですか | 0.934 | 表示 | 非表示 |
| 書類はいつまでに提出すればよいでしょうか | 0.901 | 表示 | 非表示 |
| 賃金台帳には何を記載する必要がありますか | 0.748 | 非表示 | 非表示 |

3行目のように**主語が落ちた質問**は、同じ制度の中では拾いたいが、
制度をまたぐと誤りになる。この線引きが2段階に分けた理由。

⚠️ `app_settings` は年度で分けていないため、**しきい値は R7 と R8 で共有される**
（同じSupabaseを使っているため）。判定の性質は年度に依存しないので意図的にそうしている。

しきい値は管理画面「⚖️ 修正事例」→「🎚 表示のしきい値とヒット状況」から変更できる。
`app_settings` テーブルに保存され、コードの既定値より優先される。
同じ画面に `ruling_hits` の記録が一覧表示されるので、実際のスコアを見ながら決める。
アプリ側で60秒キャッシュしているため、変更の反映には最大1分かかる。

適用範囲は登録後に「⚖️ 修正事例」の編集フォームから変更できる。

**表示ルール**

- しきい値を通過したものだけを対象とする（件数を埋めるために無関係な事例を出さない）
- 本文直下に `SHOW_LIMIT`（既定2件）。残りは「他N件の関連事例があります」で折りたたむ
- 該当なしのときは枠ごと表示しない
- 回答が `SNIPPET_CHARS`（既定300字）を超えたら「続きを読む」で折りたたむ
- 並び順はスコア降順。同点付近は新しい事例を優先（制度改正で新しい方が正しいため）
- **AIの元回答（誤っていた方）は現場に表示しない。** 管理画面でのみ確認できる

**ログ（ruling_hits）**

しきい値未満の候補も含めて上位 `LOG_CANDIDATES` 件を記録する。
しきい値と表示件数を実データで調整するため、および誤爆の検知のため。
管理画面の一覧には事例ごとの表示回数が出る（多すぎる＝誤爆の疑い）。

**年度分離**

`admin_rulings.app_year` で R7 / R8 を分離する。会話履歴と同じ考え方で、
年度違いの事例が表示されると制度改正事故になるため必須。

---

## 7. CSS・UI仕様

### PC/スマホ分離CSS
```css
/* PCはヘッダー全体を非表示 */
@media (min-width: 769px) {
    header[data-testid="stHeader"] { display: none !important; }
}
/* スマホはヘッダー表示（ハンバーガーメニューのため） */

/* 全デバイス共通：フッター・バッジ等を非表示 */
footer { display: none !important; }
#MainMenu { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stDeployButton"] { display: none !important; }
[data-testid="stToolbarActions"] { display: none !important; }
```

### ⚠️ CSS注意事項
- `[data-testid="stSidebarCollapsedControl"] { display: flex !important; }` を追加するとスマホのレイアウトが完全に破損する。**絶対に追加しないこと。**
- メディアクエリを使う場合は、グローバルルールとの干渉に注意。
- CSSは `st.set_page_config()` の直後に配置すること（全画面に適用するため）。

### Streamlit 1.43.0のハンバーガーボタンのdata-testid
`[data-testid="stExpandSidebarButton"]`（旧バージョンは `stSidebarCollapsedControl`）

---

## 8. ビルドスクリプト（knowledge → JSON生成）

`domains/[ドメイン]/knowledge/` フォルダ内のPDFから各JSONを生成するスクリプトが存在する。
- `build_rule_knowledge.py`：basic_rules.json を生成（Gemini APIを使用）
- `create_chunks.py`：pdf_chunks.json を生成
- `PAGE_BATCH_SIZE = 10`：JSON切り詰め防止のため10ページずつ処理

---

## 9. 未実装機能（実装予定）

### Googleスプレッドシートへの質問ログ記録
ユーザーの質問を記録する機能（実装保留中）。

**仕様：**
- 記録内容：日時、制度名、様式名、質問内容（各カラム）
- 保存先：Googleスプレッドシート

**実装方針：**
```python
def log_question(domain_name: str, form_name: str, question: str):
    try:
        from google.oauth2.service_account import Credentials
        import gspread
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(st.secrets["SPREADSHEET_ID"]).sheet1
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now, domain_name, form_name, question])
    except Exception:
        pass  # ログ失敗してもアプリは継続
```

**追加が必要なもの：**
- `requirements.txt` に `gspread` を追加
- Streamlit Cloud Secrets に `gcp_service_account`（JSON）と `SPREADSHEET_ID` を追加
- `send_and_stream()` 内でユーザー質問送信時に `log_question()` を呼び出す

---

## 10. 既知の問題・注意事項

| 問題 | 原因 | 対処 |
|------|------|------|
| スマホで開けない | Streamlit 1.55.0のバグ | `streamlit==1.43.0` に固定 |
| JSON切り詰め | build時にGeminiが大量ページを一度に処理 | `PAGE_BATCH_SIZE=10` で分割処理 |
| NoneType エラー | Gemini 2.5 Flashがcontent=NoneのチャンクをStream送信 | `if not content or not content.parts: continue` で対処済み |
| スマホでハンバーガー非表示 | `header`を全非表示にしていた | PC/スマホでメディアクエリ分離 |
