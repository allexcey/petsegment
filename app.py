import streamlit as st
import uuid
import re
import yaml as pyyaml
import requests
import time
from supabase import create_client, Client

st.set_page_config(page_title="PetSegment AI", page_icon="🐾", layout="centered")

st.markdown("""<style>
div.stDownloadButton > button {
    background-color: #F4B6BE !important;
    color: #5a2f34 !important;
    border: none !important;
    width: 100%;
}
div.stDownloadButton > button:hover {
    background-color: #f0a0aa !important;
}


.welcome-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 60vh;
    text-align: center;
    padding: 2rem 1rem;
}

.welcome-title {
    font-size: 2.2rem;
    font-weight: 600;
    color: #1a1a1a;
    margin-bottom: 0.5rem;
    line-height: 1.3;
}

.welcome-subtitle {
    font-size: 1rem;
    color: #888;
    margin-bottom: 2rem;
}

@keyframes pulse-dot {
    0%, 80%, 100% { opacity: 0.2; transform: scale(0.8); }
    40% { opacity: 1; transform: scale(1); }
}
.retry-dots span {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin: 0 3px;
    background-color: #e07080;
    border-radius: 50%;
    animation: pulse-dot 1.4s infinite ease-in-out;
}
.retry-dots span:nth-child(2) { animation-delay: 0.2s; }
.retry-dots span:nth-child(3) { animation-delay: 0.4s; }
</style>""", unsafe_allow_html=True)

MODES = {
    "ads":      "Реклама",
    "reports":  "Отчёты",
    "channels": "Рассылки",
}

RETRY_DELAYS = [1, 3, 5]
MAX_RETRIES = len(RETRY_DELAYS)

@st.cache_resource
def init_supabase() -> Client:
    return create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_ANON_KEY"]
    )

supabase = init_supabase()

# ── Auth ──────────────────────────────────────────────────────────────────────
if "user" not in st.session_state:
    st.session_state.user = None
    st.session_state.session = None

if not st.session_state.user:
    st.title("🐾 PetSegment AI")
    st.subheader("Вход")
    with st.form("login"):
        email    = st.text_input("Email")
        password = st.text_input("Пароль", type="password")
        submit   = st.form_submit_button("Войти")
    if submit:
        try:
            resp = supabase.auth.sign_in_with_password({"email": email, "password": password})
            st.session_state.user    = resp.user
            st.session_state.session = resp.session
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка входа: {e}")
    st.stop()

user    = st.session_state.user
session = st.session_state.session

# ── Sidebar ───────────────────────────────────────────────────────────────────
mode = "channels"  # единственный активный режим

with st.sidebar:
    st.markdown("### 🐾 PetSegment AI")
    st.caption(user.email)
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("**Тип сегмента**")
    st.markdown("""
<div style="display:flex;flex-direction:column;gap:4px;">
    <div style="padding:10px 12px;border-radius:8px;background:#FEF0F1;display:flex;align-items:center;justify-content:space-between;">
        <span style="font-size:16px;">Рассылки</span>
    </div>
    <div style="padding:10px 12px;border-radius:8px;display:flex;align-items:center;justify-content:space-between;opacity:0.4;cursor:not-allowed;">
        <span style="font-size:16px;">Реклама</span>
        <span style="font-size:11px;background:#e0e0e0;color:#666;padding:2px 7px;border-radius:10px;">скоро</span>
    </div>
    <div style="padding:10px 12px;border-radius:8px;display:flex;align-items:center;justify-content:space-between;opacity:0.4;cursor:not-allowed;">
        <span style="font-size:16px;">Отчёты</span>
        <span style="font-size:11px;background:#e0e0e0;color:#666;padding:2px 7px;border-radius:10px;">скоро</span>
    </div>
</div>
""", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("Выйти", type="secondary", use_container_width=False):
        supabase.auth.sign_out()
        st.session_state.user    = None
        st.session_state.session = None
        st.rerun()

# ── Session state init ────────────────────────────────────────────────────────
for key, val in [
    ("messages", []),
    ("generate_prompt", None),
    ("failed_prompt", None),      # последний упавший промпт — для кнопки повторить
]:
    if key not in st.session_state:
        st.session_state[key] = val

# ── on_click callback для кнопки «Повторить» ─────────────────────────────────
def on_retry_click():
    # Переносим failed_prompt в очередь генерации
    st.session_state.generate_prompt = st.session_state.failed_prompt
    st.session_state.failed_prompt = None
    # Убираем последнее сообщение об ошибке из истории
    if st.session_state.messages and st.session_state.messages[-1].get("error"):
        st.session_state.messages.pop()

# ── Helper ────────────────────────────────────────────────────────────────────
def call_edge_function(prompt, mode, access_token):
    supabase_url = st.secrets["SUPABASE_URL"]
    func_name    = st.secrets["EDGE_FUNCTION_NAME"]
    url          = f"{supabase_url}/functions/v1/{func_name}"
    try:
        resp = requests.post(
            url,
            json={"message": prompt, "mode": mode},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            timeout=60,
        )
        data = resp.json()
        if data.get("error"):
            return None, data["error"]
        return data, None
    except Exception as e:
        return None, str(e)


def generate_segment(prompt, mode):
    """Runs with retries inside st.chat_message('assistant') context."""
    status = st.empty()
    data = None

    for attempt in range(MAX_RETRIES + 1):
        if attempt == 0:
            status.markdown("⏳ Генерирую сегмент...")
        else:
            status.markdown(
                f"Сервис временно недоступен — повторяю попытку {attempt} из {MAX_RETRIES}…"
                f'<div class="retry-dots"><span></span><span></span><span></span></div>',
                unsafe_allow_html=True,
            )
            time.sleep(RETRY_DELAYS[attempt - 1])

        data, _ = call_edge_function(prompt, mode, session.access_token)
        if data is not None:
            break

    status.empty()

    if data is None:
        st.error("Сервис временно недоступен — попробуйте ещё раз")
        # Сохраняем промпт для кнопки — callback прочитает его из failed_prompt
        st.session_state.failed_prompt = prompt
        st.button(
            "🔄 Повторить",
            key="retry_btn",
            on_click=on_retry_click,
        )
        st.session_state.messages.append({
            "id":      str(uuid.uuid4()),
            "role":    "assistant",
            "error":   "Сервис временно недоступен — попробуйте ещё раз",
            "segment": "",
        })
        return

    # ── Успех ─────────────────────────────────────────────────────────────────
    st.session_state.failed_prompt = None

    yaml_text    = data.get("yaml", "")
    segment_json = data.get("segment", "")

    try:
        config_parsed = pyyaml.safe_load(yaml_text)
        seg_name = config_parsed.get("name", "segment") if config_parsed else "segment"
    except:
        config_parsed = None
        seg_name = "segment"

    slug = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ]', '_', seg_name).strip('_')
    channels = config_parsed.get("calculate_channels", []) if config_parsed else []
    channels_str = ", ".join(channels) if channels else "—"

    st.markdown(f"✔️ Ваш сегмент готов: **{seg_name}**")
    st.caption(f"{MODES.get(mode, mode)} · {channels_str}")

    if segment_json:
        st.download_button(
            label="Скачать сегмент",
            data=segment_json,
            file_name=f"{slug}.segment",
            mime="application/json",
            key=f"dl_new_{uuid.uuid4()}",
        )
    else:
        st.warning("Не удалось собрать сегмент — обратитесь к администратору")

    st.session_state.messages.append({
        "id":         str(uuid.uuid4()),
        "role":       "assistant",
        "name":       seg_name,
        "slug":       slug,
        "mode_label": f"{MODES.get(mode, mode)} · {channels_str}",
        "segment":    segment_json,
    })


# ── Welcome screen ────────────────────────────────────────────────────────────
if not st.session_state.messages and not st.session_state.generate_prompt:
    st.markdown(
        """
        <div class="welcome-container">
            <div class="welcome-title">Создайте свой сегмент</div>
            <div class="welcome-subtitle">Опишите аудиторию — AI соберёт сегмент для CDP</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ── Chat history ──────────────────────────────────────────────────────────────
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            if msg.get("error"):
                st.error(msg["error"])
            else:
                st.markdown(f"✔️ Ваш сегмент готов: **{msg.get('name', 'Сегмент')}**")
                st.caption(msg.get("mode_label", ""))
                if msg.get("segment"):
                    st.download_button(
                        label="Скачать сегмент",
                        data=msg["segment"],
                        file_name=f"{msg.get('slug', 'segment')}.segment",
                        mime="application/json",
                        key=f"dl_{msg['id']}",
                    )
        else:
            st.markdown(msg["content"])

# Показываем кнопку «Повторить» под историей, если последний ответ — ошибка
if (
    st.session_state.messages
    and st.session_state.messages[-1].get("error")
    and st.session_state.failed_prompt
):
    st.button(
        "🔄 Повторить",
        key="retry_btn_history",
        on_click=on_retry_click,
    )

# ── Определяем активный промпт ───────────────────────────────────────────────
active_prompt = None

# Сначала проверяем очередь (retry или программный запуск)
if st.session_state.generate_prompt:
    active_prompt = st.session_state.generate_prompt
    st.session_state.generate_prompt = None

# Новый ввод от пользователя
prompt = st.chat_input("Опишите сегмент (например: покупали корм для собак за последние 90 дней)...")
if prompt:
    active_prompt = prompt
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

if active_prompt:
    with st.chat_message("assistant"):
        generate_segment(active_prompt, mode)
