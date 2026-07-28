"""
admin.py  –  管理画面 UI（ユーザー管理・会話履歴閲覧・利用統計・修正事例）
"""
import streamlit as st
from auth import require_admin, hash_password
from db import (
    get_all_users, create_user, update_password, set_user_active, delete_user,
    get_all_user_stats,
    get_all_conversations_by_user, get_messages_by_conversation,
    create_ruling, get_rulings_for_admin, get_active_rulings, update_ruling,
    set_ruling_active, delete_ruling, get_recent_ruling_hits,
    get_setting, set_setting,
)
from rulings import (
    encode_embedding, embed_text, find_similar_for_admin, get_client,
    EMBED_THRESHOLD, SETTING_KEY_THRESHOLD,
    GLOBAL_THRESHOLD, SETTING_KEY_GLOBAL_THRESHOLD,
)


def _year_label(app_year: str) -> str:
    return {"R7": "令和7年度版", "R8": "令和8年度版"}.get(app_year or "R7", app_year or "R7")


# 様式を選ばずに質問しているときの selected_form の値（app.py の form_options と対応）。
# これを「様式」として修正事例に保存すると、様式を選んでいない利用者にしか
# 表示されない事例になってしまうため、適用範囲の判定で特別扱いする。
GENERIC_FORM_NAME = "全般（様式を特定しない）"


# 管理画面のナビゲーション項目（st.radio で切替。プログラムからの遷移に対応）
NAV_USERS = "👥 ユーザー管理"
NAV_CONVERSATIONS = "💬 会話履歴閲覧"
NAV_STATS = "📊 利用統計"
NAV_RULINGS = "⚖️ 修正事例"
NAV_OPTIONS = [NAV_USERS, NAV_CONVERSATIONS, NAV_STATS, NAV_RULINGS]


def render_admin_page():
    """管理画面のメインレンダリング関数（app.py から呼び出す）"""
    require_admin()

    # ── ヘッダー & 戻るボタン ──────────────────────────────────
    col_h1, col_h2 = st.columns([4, 1])
    with col_h1:
        st.markdown("## 🔧 管理画面")
    with col_h2:
        if st.button("← アプリに戻る", use_container_width=True):
            st.session_state.app_state = "setup"
            st.rerun()

    st.caption(f"ログイン中: {st.session_state.display_name}（管理者）")
    st.divider()

    # ── ナビゲーション ──
    # 他ビュー（利用統計など）からのジャンプ要求があれば、ラジオ生成前に反映する
    if "admin_nav_pending" in st.session_state:
        st.session_state["admin_nav"] = st.session_state.pop("admin_nav_pending")

    nav = st.radio(
        "メニュー",
        NAV_OPTIONS,
        key="admin_nav",
        horizontal=True,
        label_visibility="collapsed",
    )

    if nav == NAV_USERS:
        _render_user_management()
    elif nav == NAV_CONVERSATIONS:
        _render_conversation_viewer()
    elif nav == NAV_RULINGS:
        _render_rulings_manager()
    else:
        _render_usage_stats()


# =============================================================
# タブ1: ユーザー管理
# =============================================================
def _render_user_management():
    # ── 新規ユーザー追加 ──
    with st.expander("＋ 新しいユーザーを追加"):
        with st.form("add_user_form", clear_on_submit=True):
            new_username     = st.text_input("ログインID（英数字）")
            new_display      = st.text_input("表示名")
            new_password     = st.text_input("パスワード", type="password")
            new_is_admin     = st.checkbox("管理者権限を付与")
            add_submitted    = st.form_submit_button("追加", type="primary")

        if add_submitted:
            if not new_username or not new_password:
                st.error("ログインIDとパスワードは必須です。")
            else:
                try:
                    create_user(new_username, new_display or new_username,
                                hash_password(new_password), new_is_admin)
                    st.success(f"✅ ユーザー「{new_username}」を追加しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"追加失敗：{e}")

    st.divider()

    # ── ユーザー一覧 ──
    users = get_all_users()
    if not users:
        st.info("ユーザーが登録されていません。")
        return

    # 会社名・IDで検索
    search = st.text_input(
        "🔍 会社名・ログインIDで検索",
        key="user_mgmt_search",
        placeholder="社名やIDの一部を入力（空欄で全員表示）",
    )
    if search:
        s = search.lower()
        users = [
            u for u in users
            if s in u["display_name"].lower() or s in u["username"].lower()
        ]
        if not users:
            st.warning("該当するユーザーが見つかりません。")
            return

    for user in users:
        uid = user["id"]
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
            c1.markdown(f"**{user['display_name']}**  \n`{user['username']}`")
            c2.write("👑 管理者" if user["is_admin"] else "一般")
            c3.write("✅ 有効" if user["is_active"] else "⛔ 無効")

            with c4:
                btn_col1, btn_col2, btn_col3 = st.columns(3)

                # パスワード変更
                with btn_col1:
                    if st.button("PW変更", key=f"pw_btn_{uid}", use_container_width=True):
                        st.session_state[f"pw_edit_{uid}"] = True

                # 有効/無効切替
                with btn_col2:
                    toggle_label = "無効化" if user["is_active"] else "有効化"
                    if st.button(toggle_label, key=f"toggle_{uid}", use_container_width=True):
                        set_user_active(uid, not bool(user["is_active"]))
                        st.rerun()

                # 削除
                with btn_col3:
                    if st.button("削除", key=f"del_{uid}", use_container_width=True,
                                 type="primary" if False else "secondary"):
                        st.session_state[f"del_confirm_{uid}"] = True

            # パスワード変更フォーム（展開時）
            if st.session_state.get(f"pw_edit_{uid}"):
                with st.form(f"pw_form_{uid}"):
                    new_pw = st.text_input("新しいパスワード", type="password")
                    pw_ok  = st.form_submit_button("変更する")
                if pw_ok:
                    if new_pw:
                        update_password(uid, hash_password(new_pw))
                        st.session_state.pop(f"pw_edit_{uid}", None)
                        st.success("パスワードを変更しました。")
                        st.rerun()
                    else:
                        st.error("パスワードを入力してください。")

            # 削除確認（展開時）
            if st.session_state.get(f"del_confirm_{uid}"):
                st.warning(f"「{user['display_name']}」を削除します。会話履歴も全て削除されます。本当によろしいですか？")
                d1, d2 = st.columns(2)
                with d1:
                    if st.button("削除する", key=f"del_yes_{uid}", type="primary", use_container_width=True):
                        delete_user(uid)
                        st.session_state.pop(f"del_confirm_{uid}", None)
                        st.success("削除しました。")
                        st.rerun()
                with d2:
                    if st.button("キャンセル", key=f"del_no_{uid}", use_container_width=True):
                        st.session_state.pop(f"del_confirm_{uid}", None)
                        st.rerun()


# =============================================================
# タブ2: 会話履歴閲覧
# =============================================================
def _render_conversation_viewer():
    users = get_all_users()
    if not users:
        st.info("ユーザーが登録されていません。")
        return

    user_by_id = {u["id"]: u for u in users}

    # ── 会社名・IDで検索 ──
    search = st.text_input(
        "🔍 会社名・ログインIDで検索",
        key="conv_search",
        placeholder="社名やIDの一部を入力（空欄で全員表示）",
    )
    if search:
        s = search.lower()
        filtered = [
            u for u in users
            if s in u["display_name"].lower() or s in u["username"].lower()
        ]
    else:
        filtered = users

    if not filtered:
        st.warning("該当するユーザーが見つかりません。検索条件を変えてください。")
        return

    option_ids = [u["id"] for u in filtered]

    # ── 利用統計からのジャンプ先ユーザーを選択状態にする（ウィジェット生成前）──
    jump_id = st.session_state.pop("conv_target_user_id", None)
    if jump_id is not None and jump_id in option_ids:
        st.session_state["conv_user_select"] = jump_id
    # 保存済みの選択が現在の候補に無ければリセット（検索で絞られた場合など）
    if st.session_state.get("conv_user_select") not in option_ids:
        st.session_state.pop("conv_user_select", None)

    selected_id = st.selectbox(
        "ユーザーを選択",
        options=option_ids,
        format_func=lambda uid: f"{user_by_id[uid]['display_name']} ({user_by_id[uid]['username']})",
        key="conv_user_select",
    )
    selected_user = user_by_id[selected_id]

    convs = get_all_conversations_by_user(selected_user["id"], limit=100)
    if not convs:
        st.info("このユーザーの会話履歴はありません。")
        return

    selected_conv = st.selectbox(
        "会話を選択",
        options=convs,
        format_func=lambda c: f"[{_year_label(c.get('app_year'))}] {c['title']}　（{c['updated_at'][:10]}）",
    )

    if not selected_conv:
        return

    st.caption(
        f"年度: {_year_label(selected_conv.get('app_year'))}　"
        f"制度: {selected_conv['domain_key']}　様式: {selected_conv['form_name']}"
    )
    st.divider()

    messages = get_messages_by_conversation(selected_conv["id"])
    if not messages:
        st.info("メッセージがありません。")

    if st.session_state.pop("ruling_flash", None):
        st.success("修正事例を登録しました。")

    # AI回答の直前にあるユーザー発言を、その回答への「質問」とみなす
    prev_question = ""
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            st.caption(msg["created_at"])

            if msg["role"] == "assistant":
                target = st.session_state.get("ruling_target") or {}
                if target.get("message_id") == msg["id"]:
                    _render_ruling_form(target)
                elif st.button("✏️ この回答を修正", key=f"fix_{msg['id']}"):
                    st.session_state["ruling_target"] = {
                        "message_id": msg["id"],
                        "conversation_id": selected_conv["id"],
                        "app_year": selected_conv.get("app_year") or "R7",
                        "domain_key": selected_conv["domain_key"],
                        "form_name": selected_conv["form_name"],
                        "question": prev_question,
                        "original_answer": msg["content"],
                    }
                    st.session_state.pop("ruling_similar", None)
                    st.rerun()

        if msg["role"] == "user":
            prev_question = msg["content"]


def _clear_ruling_target() -> None:
    st.session_state.pop("ruling_target", None)
    st.session_state.pop("ruling_similar", None)


def _render_ruling_form(target: dict) -> None:
    """AI回答を修正事例として登録するフォーム（会話履歴の該当メッセージ直下に出す）"""
    st.divider()
    st.markdown("**✏️ 修正事例として登録**")

    # 重複登録の防止。表示側で調整するより、登録時に潰すほうが確実。
    if "ruling_similar" not in st.session_state:
        try:
            similar = find_similar_for_admin(
                get_client(), target["question"], get_active_rulings(target["app_year"])
            )
        except Exception:
            similar = []
        st.session_state["ruling_similar"] = [
            {"id": r["id"], "q": r["question_text"], "a": r["corrected_answer"]}
            for r in similar
        ]

    for s in st.session_state["ruling_similar"]:
        st.warning(f"似た事例が登録済みです（#{s['id']}）：{s['q'][:60]}")
        if st.checkbox("内容を確認する", key=f"peek_{target['message_id']}_{s['id']}"):
            st.markdown(f"質問：{s['q']}")
            st.markdown(f"回答：{s['a']}")
    if st.session_state["ruling_similar"]:
        st.caption("重複する場合は登録せず、「⚖️ 修正事例」画面で既存を編集してください。")

    with st.form(f"ruling_form_{target['message_id']}"):
        st.caption(
            "質問文は検索キーになります。特定の会社の事情に依存した表現は、"
            "他のケースでも使える言い回しに直してください。"
        )
        q = st.text_area("質問文", value=target["question"], height=80)
        a = st.text_area("正しい回答（現場にはこの内容が表示されます）", height=180)
        # 元の会話で様式を選んでいなかった場合、「様式のみ」で登録すると
        # 「全般」を選んでいる利用者にしか表示されなくなる。選択肢自体を出さない。
        form_name = (target["form_name"] or "").strip()
        form_is_generic = form_name in ("", GENERIC_FORM_NAME)
        scope_options = (
            ["この制度全体", "全体共通"] if form_is_generic
            else ["この制度・様式のみ", "この制度全体", "全体共通"]
        )
        scope = st.radio(
            "適用範囲",
            scope_options,
            index=scope_options.index("この制度全体"),  # 迷ったら制度全体が無難
            horizontal=True,
        )
        if form_is_generic:
            st.caption(
                "※ 元の会話で様式を選んでいなかったため、「様式のみ」は選べません"
                "（登録しても、様式を選んでいない利用者にしか表示されないため）。"
            )
        else:
            st.caption(
                f"「この制度・様式のみ」は、利用者が様式「{form_name}」を選んでいるときだけ"
                "表示されます。記入欄など様式固有の話でなければ「この制度全体」を推奨します。"
            )
        st.caption(
            f"⚠️「全体共通」を選ぶと、{target['domain_key']} 以外のすべての制度の質問にも"
            "表示される可能性があります。判定は厳しくなりますが、"
            "特定の制度の話であれば「この制度全体」を選んでください。"
        )
        memo = st.text_input("メモ（任意・現場には表示されません）")

        c1, c2 = st.columns(2)
        with c1:
            submitted = st.form_submit_button("登録", type="primary", use_container_width=True)
        with c2:
            cancelled = st.form_submit_button("キャンセル", use_container_width=True)

    if cancelled:
        _clear_ruling_target()
        st.rerun()

    if submitted:
        if not q.strip() or not a.strip():
            st.error("質問文と正しい回答は必須です。")
            return
        if scope == "この制度・様式のみ":
            domain_key, form_name = target["domain_key"], target["form_name"]
        elif scope == "この制度全体":
            domain_key, form_name = target["domain_key"], ""
        else:
            domain_key, form_name = "", ""

        create_ruling(
            app_year=target["app_year"],
            question_text=q.strip(),
            corrected_answer=a.strip(),
            domain_key=domain_key,
            form_name=form_name,
            original_answer=target["original_answer"],
            comment=memo.strip(),
            source_conversation_id=target["conversation_id"],
            source_message_id=target["message_id"],
            embedding=encode_embedding(embed_text(get_client(), q.strip())),
            created_by=st.session_state.get("user_id"),
        )
        _clear_ruling_target()
        st.session_state["ruling_flash"] = True
        st.rerun()


# =============================================================
# タブ3: 利用統計
# =============================================================
def _render_usage_stats():
    try:
        import pandas as pd
    except ImportError:
        st.error("pandas がインストールされていません。`pip install pandas` を実行してください。")
        return

    stats = get_all_user_stats()
    if not stats:
        st.info("データがありません。")
        return

    # ── 並び替えUI ──
    SORT_OPTIONS = {
        "登録日順（デフォルト）": None,
        "表示名順":             "display_name",
        "最終ログイン順":       "last_login_at",
        "会話数順":             "total_conversations",
        "メッセージ数順":       "total_messages",
    }
    c1, c2 = st.columns([3, 1])
    with c1:
        sort_label = st.selectbox("並び替え", list(SORT_OPTIONS.keys()), key="stats_sort_key")
    with c2:
        descending = st.toggle("降順", value=True, key="stats_sort_desc")

    sort_field = SORT_OPTIONS[sort_label]
    stats_sorted = list(stats)
    if sort_field in ("total_conversations", "total_messages"):
        stats_sorted.sort(key=lambda s: s.get(sort_field) or 0, reverse=descending)
    elif sort_field:
        stats_sorted.sort(key=lambda s: str(s.get(sort_field) or ""), reverse=descending)

    # 表示順に対応するユーザーIDリスト（行選択→ジャンプ用）
    uids = [s["id"] for s in stats_sorted]

    df = pd.DataFrame(stats_sorted)
    df = df.rename(columns={
        "username":            "ログインID",
        "display_name":        "表示名",
        "is_active":           "有効",
        "last_login_at":       "最終ログイン",
        "total_conversations": "会話数",
        "total_messages":      "メッセージ数",
    })
    df["有効"] = df["有効"].map({1: "✅", 0: "⛔"})
    df = df.drop(columns=["id"], errors="ignore")
    df = df[["表示名", "ログインID", "有効", "最終ログイン", "会話数", "メッセージ数"]]

    st.caption("💡 行を選択すると、その会社の会話履歴へ移動できます。")
    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    sel = getattr(event, "selection", None)
    rows = list(sel.rows) if sel and getattr(sel, "rows", None) else []
    if rows:
        pos = rows[0]
        target_uid = uids[pos]
        target_name = stats_sorted[pos]["display_name"]
        if st.button(
            f"💬 「{target_name}」の会話履歴に移動する",
            type="primary",
            use_container_width=True,
        ):
            st.session_state["admin_nav_pending"] = NAV_CONVERSATIONS
            st.session_state["conv_target_user_id"] = target_uid
            st.rerun()


# =============================================================
# タブ4: 修正事例（管理者による裁定）
# =============================================================
def _scope_label(r: dict) -> str:
    if r.get("form_name"):
        return f"{r['domain_key']} / {r['form_name']}"
    if r.get("domain_key"):
        return f"{r['domain_key']}（制度全体）"
    return "全体共通"


def _current_threshold(key: str = SETTING_KEY_THRESHOLD, default: float = EMBED_THRESHOLD) -> float:
    try:
        return float(get_setting(key, str(default)))
    except (ValueError, TypeError):
        return default


def _domain_options() -> list[str]:
    """適用範囲の選択肢。domains フォルダの制度名をそのまま使う。"""
    import os
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domains")
    try:
        return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except OSError:
        return []


def _render_ruling_tuning():
    """しきい値の調整と、実際のヒット状況の確認。
    再デプロイなしで調整できるようにしている（数値は運用しながら詰める前提のため）。
    """
    with st.expander("🎚 表示のしきい値とヒット状況"):
        st.caption(
            "スコアがしきい値以上の事例だけが現場に表示されます。"
            "下げると拾いやすくなりますが、関係のない質問にも出やすくなります。"
        )

        current = _current_threshold()
        current_global = _current_threshold(SETTING_KEY_GLOBAL_THRESHOLD, GLOBAL_THRESHOLD)

        new_value = st.slider(
            "しきい値：制度を指定した事例",
            min_value=0.50, max_value=0.98, value=current, step=0.01,
            help="日本語は文体が似ているだけでスコアが上がるため、低くしすぎると誤爆します。",
        )
        st.caption(f"現在の設定値: {current}（既定値 {EMBED_THRESHOLD}）")

        new_global = st.slider(
            "しきい値：全体共通の事例",
            min_value=0.50, max_value=0.99, value=current_global, step=0.01,
            help="全体共通の事例はどの制度の質問にも表示されるため、より厳しくします。",
        )
        st.caption(
            f"現在の設定値: {current_global}（既定値 {GLOBAL_THRESHOLD}）　"
            "スコアは質問文だけで決まり制度は考慮しないため、"
            "制度を指定しない事例は他の制度の質問にも届いてしまいます。"
        )

        if st.button("保存", type="primary"):
            set_setting(SETTING_KEY_THRESHOLD, str(new_value))
            set_setting(SETTING_KEY_GLOBAL_THRESHOLD, str(new_global))
            st.success(f"しきい値を {new_value} / {new_global} に変更しました。")
        st.caption("※ 反映まで最大1分かかります（アプリ側でキャッシュしているため）")

        st.divider()
        st.markdown("**直近のヒット状況**")
        st.caption(
            "しきい値を通らなかった候補も含めて記録しています。"
            "「表示」が✅なのに関係のない質問なら、しきい値を上げてください。"
        )

        hits = get_recent_ruling_hits(limit=100)
        if not hits:
            st.info("まだ記録がありません。")
            return

        import pandas as pd
        df = pd.DataFrame(hits)
        df["表示"] = df["shown"].map({1: "✅", 0: ""})
        df["スコア"] = df["score"].map(lambda v: f"{v:.3f}")
        df = df.rename(columns={
            "created_at":    "日時",
            "question":      "現場の質問",
            "question_text": "ヒットした事例",
            "method":        "方式",
        })
        st.dataframe(
            df[["日時", "現場の質問", "ヒットした事例", "スコア", "表示", "方式"]],
            use_container_width=True, hide_index=True,
        )


def _render_rulings_manager():
    st.markdown("### ⚖️ 修正事例")
    st.caption(
        "AIの回答が実務的に誤っていた場合に、管理者が正しい回答を登録したものです。"
        "現場の画面では、似た質問が出たときに回答の下へ参考として表示されます。"
        "登録は「💬 会話履歴閲覧」で対象の回答を開き、「✏️ この回答を修正」から行います。"
    )

    _render_ruling_tuning()

    # 類似判定がどちらの方式で動いているかを本番環境で確認するための診断
    with st.expander("🔧 類似判定の方式を確認"):
        st.caption(
            "「似た質問」の判定は埋め込みベクトルで行います。"
            "APIキーがこのモデルに対応していない場合は、自動的にバイグラム一致に切り替わります"
            "（言い換えを拾えなくなるため精度は落ちます）。"
        )
        if st.button("接続を確認する"):
            vec = embed_text(get_client(), "対象労働者の要件を教えてください")
            if vec:
                st.success(f"埋め込みベクトル方式で動作しています（{len(vec)}次元）。")
            else:
                st.warning(
                    "埋め込みモデルを利用できないため、バイグラム一致で動作しています。"
                    "APIキーの対応状況を確認してください。"
                )

    rulings = get_rulings_for_admin()
    if not rulings:
        st.info("まだ修正事例が登録されていません。")
        return

    search = st.text_input(
        "🔍 質問文・回答で検索",
        key="ruling_search",
        placeholder="キーワードの一部を入力（空欄で全件表示）",
    )
    if search:
        s = search.lower()
        rulings = [
            r for r in rulings
            if s in r["question_text"].lower() or s in r["corrected_answer"].lower()
        ]
    show_inactive = st.toggle("無効化した事例も表示", value=False, key="ruling_show_inactive")
    if not show_inactive:
        rulings = [r for r in rulings if r["is_active"] == 1]

    if not rulings:
        st.warning("該当する事例がありません。")
        return

    st.caption(f"{len(rulings)}件")

    for r in rulings:
        status = "" if r["is_active"] == 1 else "⛔ "
        shown_count = r.get("shown_count") or 0
        header = (
            f"{status}[{_year_label(r['app_year'])}] {r['question_text'][:40]}"
            f"　（表示{shown_count}回）"
        )
        with st.expander(header):
            st.caption(
                f"適用範囲: {_scope_label(r)}　登録: {r.get('created_by_name') or '不明'} / "
                f"{r['created_at'][:10]}　更新: {r['updated_at'][:10]}"
            )
            if shown_count >= 20:
                st.warning(
                    "表示回数が多い事例です。関係のない質問にも出ていないか確認してください。"
                )

            with st.form(f"ruling_edit_{r['id']}"):
                q = st.text_area("質問文", value=r["question_text"], height=80)
                a = st.text_area("正しい回答", value=r["corrected_answer"], height=180)
                memo = st.text_input("メモ（現場には表示されません）", value=r["comment"])

                # 適用範囲は登録後に間違いに気づくことがあるので変更できるようにする
                domains = _domain_options()
                has_domain = bool((r["domain_key"] or "").strip())
                scope_mode = st.radio(
                    "適用範囲",
                    ["制度を指定", "全体共通"],
                    index=0 if has_domain else 1,
                    horizontal=True,
                    key=f"scope_mode_{r['id']}",
                )
                dom_index = domains.index(r["domain_key"]) if r["domain_key"] in domains else 0
                new_domain = st.selectbox(
                    "制度（「制度を指定」を選んだ場合）",
                    options=domains or [""],
                    index=dom_index,
                    key=f"scope_dom_{r['id']}",
                )
                keep_form = False
                cur_form = (r["form_name"] or "").strip()
                if cur_form and cur_form != GENERIC_FORM_NAME:
                    keep_form = st.checkbox(
                        f"様式「{cur_form}」にも限定する（この様式を選んでいるときだけ表示）",
                        value=True, key=f"scope_form_{r['id']}",
                    )
                st.caption(
                    "「全体共通」はあらゆる制度の質問に表示されます。"
                    "そのぶん厳しいしきい値で判定されます。"
                )
                saved = st.form_submit_button("保存", type="primary")

            if saved:
                if not q.strip() or not a.strip():
                    st.error("質問文と正しい回答は必須です。")
                elif scope_mode == "制度を指定" and not new_domain:
                    st.error("制度を選択してください。")
                else:
                    if scope_mode == "全体共通":
                        dk, fn = "", ""
                    else:
                        dk = new_domain
                        fn = r["form_name"] if keep_form else ""
                    update_ruling(
                        r["id"], q.strip(), a.strip(),
                        domain_key=dk, form_name=fn,
                        comment=memo.strip(),
                        embedding=encode_embedding(embed_text(get_client(), q.strip())),
                    )
                    st.success("保存しました。")
                    st.rerun()

            # 元のAI回答は管理者だけが見る（現場には表示しない）
            if r["original_answer"] and st.checkbox("元のAI回答を表示", key=f"orig_{r['id']}"):
                st.text(r["original_answer"])

            c1, c2 = st.columns(2)
            with c1:
                if r["is_active"] == 1:
                    if st.button("⛔ 無効化", key=f"deact_{r['id']}", use_container_width=True):
                        set_ruling_active(r["id"], False)
                        st.rerun()
                else:
                    if st.button("✅ 有効化", key=f"act_{r['id']}", use_container_width=True):
                        set_ruling_active(r["id"], True)
                        st.rerun()
            with c2:
                confirm = st.checkbox("削除を確認", key=f"delchk_{r['id']}")
                if st.button("🗑 削除", key=f"del_{r['id']}",
                             disabled=not confirm, use_container_width=True):
                    delete_ruling(r["id"])
                    st.rerun()
