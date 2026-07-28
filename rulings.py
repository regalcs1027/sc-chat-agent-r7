"""
rulings.py  –  管理者による修正事例（裁定）の類似検索

【Phase 1 の方針】
検索結果は現場の画面に「参考情報」として表示するだけで、
AIのシステムプロンプトには渡さない。
誤爆した場合にAIの回答本文が汚染されるのを避けるため。
実データ（ruling_hits）でスコア分布を確認してから Phase 2（プロンプト注入）を検討する。

【類似判定の方式】
1. 埋め込みベクトル（gemini-embedding-001）… 言い換えに強い。こちらが本命
2. バイグラム一致 … 埋め込みが使えない場合のフォールバック
   ※新規APIキーで一部モデルが 404 になる事例があるため、必ず退避経路を持たせる
"""
import json
import math
import os

from google.genai import Client, types

# =============================================================
# 調整パラメータ
#   ruling_hits に溜まったスコア分布を見ながら見直す前提の値。
# =============================================================
SHOW_LIMIT = 2          # 本文直下に表示する件数
EXPAND_LIMIT = 5        # 「他N件」の折りたたみに入れる上限
LOG_CANDIDATES = 5      # しきい値未満でもログに残す候補数（分布の把握用）
SNIPPET_CHARS = 300     # これを超える回答は折りたたむ

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768         # 既定の3072は過剰。DBサイズを抑える

# コサイン類似度のしきい値。管理画面から変更でき、DBの設定値が優先される。
# 日本語の質問文は文体が似ているだけでスコアが上がるため、低くしすぎると誤爆する。
#
# しきい値は適用範囲で2段階に分ける。スコアは質問文だけで決まり制度は考慮しないため、
# 制度を指定しない「全体共通」の事例は、どの制度の質問にも届いてしまうためである。
# 実測例: 「書類はいつまでに提出すればよいでしょうか」は
#   「キャリアアップ計画書はいつまでに提出する必要がありますか？」に対して 0.901。
#   同じ制度の中なら拾いたいが、別制度に出たら誤りなので全体共通では切る。
SETTING_KEY_THRESHOLD = "ruling_embed_threshold"
SETTING_KEY_GLOBAL_THRESHOLD = "ruling_embed_threshold_global"
EMBED_THRESHOLD = 0.85         # 制度（・様式）を指定した事例
GLOBAL_THRESHOLD = 0.95        # 全体共通の事例。ほぼ同じ言い回しのときだけ表示する
BIGRAM_THRESHOLD = 0.30        # Dice係数のしきい値（フォールバック時）

# 埋め込みモデルが使えない環境と判明したら以降は試行しない
_embed_disabled = False
_client = None


def get_client():
    """管理画面（admin.py）用の Gemini クライアント。
    app.py の client を import すると循環参照になるため、ここで別途組み立てる。
    """
    global _client
    if _client is None:
        api_key = None
        try:
            import streamlit as st
            api_key = st.secrets.get("GEMINI_API_KEY", None)
        except Exception:
            pass
        api_key = api_key or os.getenv("GEMINI_API_KEY")
        _client = Client(api_key=api_key) if api_key else False
    return _client or None


# =============================================================
# 埋め込みベクトル
# =============================================================
def embed_text(client, text: str) -> list[float] | None:
    """テキストを正規化済みベクトルに変換。失敗時は None（呼び出し側はバイグラムに退避）"""
    global _embed_disabled
    if _embed_disabled or client is None or not text.strip():
        return None
    try:
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=text,
            config=types.EmbedContentConfig(
                task_type="SEMANTIC_SIMILARITY",
                output_dimensionality=EMBED_DIM,
            ),
        )
        vec = list(resp.embeddings[0].values)
    except Exception as e:
        err = str(e)
        # モデル自体が使えないキーの場合は恒久的に無効化（毎回叩いても無駄なため）
        if "404" in err or "NOT_FOUND" in err or "PERMISSION_DENIED" in err:
            _embed_disabled = True
        return None

    # output_dimensionality を既定値から変えた場合、正規化は呼び出し側の責任
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return None
    return [v / norm for v in vec]


def encode_embedding(vec: list[float] | None) -> str | None:
    """DB(TEXT)に保存する形へ"""
    return json.dumps(vec) if vec else None


def decode_embedding(raw) -> list[float] | None:
    if not raw:
        return None
    try:
        vec = json.loads(raw)
        return vec if isinstance(vec, list) and vec else None
    except (json.JSONDecodeError, TypeError):
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    """双方とも正規化済み前提なので内積がそのままコサイン類似度"""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# =============================================================
# バイグラム一致（フォールバック）
# =============================================================
def _bigrams(text: str) -> set:
    t = "".join(text.split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _bigram_score(a: str, b: str) -> float:
    """Dice係数（0.0〜1.0）。app.py のRAGと違い、長さで正規化して比較可能にする"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


# =============================================================
# 検索本体
# =============================================================
# 様式を選ばずに質問しているときの様式名。これが「様式」として保存されている事例は、
# 様式で絞る意図ではないので制度全体として扱う（そうしないと、様式を選んでいない
# 利用者にしか表示されない事例になってしまう）。
GENERIC_FORM_NAME = "全般（様式を特定しない）"


def _in_scope(ruling: dict, domain_key: str, form_name: str) -> bool:
    """スコープ判定。空欄は「全体共通」の意味なので常に対象"""
    rd = (ruling.get("domain_key") or "").strip()
    rf = (ruling.get("form_name") or "").strip()
    if rd and rd != domain_key:
        return False
    if rf and rf != GENERIC_FORM_NAME and rf != form_name:
        return False
    return True


def search_rulings(
    client,
    question: str,
    rulings: list[dict],
    domain_key: str = "",
    form_name: str = "",
    embed_threshold: float | None = None,
    global_threshold: float | None = None,
) -> tuple[list[dict], list[tuple[int, float, bool, str]]]:
    """質問に関連する修正事例を検索する。

    戻り値:
      shown      … 画面に出す事例（スコア降順・最大 EXPAND_LIMIT 件）
      candidates … ログ記録用 [(ruling_id, score, shown, method), ...]
                   しきい値未満の候補も含む（分布把握のため）
    """
    targets = [r for r in rulings if _in_scope(r, domain_key, form_name)]
    if not targets:
        return [], []

    q_vec = embed_text(client, question)
    embed_th = EMBED_THRESHOLD if embed_threshold is None else embed_threshold
    global_th = GLOBAL_THRESHOLD if global_threshold is None else global_threshold

    scored: list[tuple[float, str, dict]] = []
    for r in targets:
        r_vec = decode_embedding(r.get("embedding")) if q_vec else None
        if q_vec and r_vec:
            score, method = _cosine(q_vec, r_vec), "embedding"
            # 制度を指定していない事例は、どの制度の質問にも届くため厳しく判定する
            threshold = embed_th if (r.get("domain_key") or "").strip() else global_th
        else:
            score, method = _bigram_score(question, r.get("question_text", "")), "bigram"
            threshold = BIGRAM_THRESHOLD
        scored.append((score, method, r))
        r["_score"] = score
        r["_passed"] = score >= threshold

    # スコア降順。同点付近は新しい事例を優先（制度改正で新しい方が正しいことが多い）
    scored.sort(key=lambda x: (x[0], x[2]["id"]), reverse=True)

    passed = [(s, m, r) for s, m, r in scored if r["_passed"]]
    shown = [r for _, _, r in passed[:EXPAND_LIMIT]]
    shown_ids = {r["id"] for r in shown}

    candidates = [
        (r["id"], round(float(s), 4), r["id"] in shown_ids, m)
        for s, m, r in scored[:LOG_CANDIDATES]
    ]
    return shown, candidates


def find_similar_for_admin(
    client,
    question: str,
    rulings: list[dict],
    limit: int = 3,
) -> list[dict]:
    """管理者が新規登録するときの重複チェック用。
    スコープもしきい値も無視して、単純に似ている順の上位を返す。
    """
    if not rulings:
        return []
    q_vec = embed_text(client, question)
    scored = []
    for r in rulings:
        r_vec = decode_embedding(r.get("embedding")) if q_vec else None
        if q_vec and r_vec:
            score = _cosine(q_vec, r_vec)
        else:
            score = _bigram_score(question, r.get("question_text", ""))
        r["_score"] = score
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for s, r in scored[:limit] if s > 0]
