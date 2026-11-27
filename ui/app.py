"""Run from the repository root with python -m streamlit run ui/app.py."""
from pathlib import Path
import sys

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.contracts import ProviderSettings
from ui import dashboard
from ui.clarification import render_clarification

st.set_page_config(page_title="Аудит требований", layout="wide")
st.title("Аудит требований")
mode = st.radio("Рабочий режим", ["Анализ документа", "Сохранённый отчёт"],
                horizontal=True, label_visibility="collapsed")
saved = mode == "Сохранённый отчёт"
report = dashboard.open_saved() if saved else dashboard.analyze()
if report is not None:
    dashboard.show_report(report, saved=saved)
    base = dashboard.load_settings()
    model = str(st.session_state.get("model", base.model)).strip() or base.model
    timeout = int(st.session_state.get("timeout_ms", base.timeout_ms))
    render_clarification(report, ProviderSettings(api_key=base.api_key, model=model, timeout_ms=timeout))
