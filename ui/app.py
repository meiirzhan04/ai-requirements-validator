"""Run from the repository root with python -m streamlit run ui/app.py."""
from pathlib import Path
import sys

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui.dashboard import analyze, open_saved, show_report

st.set_page_config(page_title="Аудит требований", layout="wide")
st.title("Аудит требований")
mode = st.radio("Рабочий режим", ["Анализ документа", "Сохранённый отчёт"],
                horizontal=True, label_visibility="collapsed")
saved = mode == "Сохранённый отчёт"
report = open_saved() if saved else analyze()
if report is not None:
    show_report(report, saved=saved)
