"""
Мелкие переиспользуемые хелперы Streamlit-интерфейса.
Используются и в app.py (дайджест КОС), и в src/appointments_ui.py (дайджест назначений).
"""
import html as html_lib


def esc(text) -> str:
    """HTML-escape пользовательского ввода для безопасного рендера в st.markdown."""
    return html_lib.escape(str(text)) if text else ""
