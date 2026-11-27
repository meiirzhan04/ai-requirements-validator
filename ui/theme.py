"""Enterprise Fintech visual system for Streamlit (light and dark theme safe, zero external CDNs)."""
import streamlit as st

READINESS_TOKENS = {
    "go": {
        "accent": "#15803D",
        "tint": "rgba(21, 128, 61, 0.08)",
        "border": "rgba(21, 128, 61, 0.32)",
        "label": "Готово к разработке",
    },
    "conditional_go": {
        "accent": "#B45309",
        "tint": "rgba(180, 83, 9, 0.09)",
        "border": "rgba(180, 83, 9, 0.34)",
        "label": "Условная готовность",
    },
    "no_go": {
        "accent": "#B91C1C",
        "tint": "rgba(185, 28, 28, 0.08)",
        "border": "rgba(185, 28, 28, 0.32)",
        "label": "Заблокировано",
    },
    "undetermined": {
        "accent": "#0F4C81",
        "tint": "rgba(15, 76, 129, 0.08)",
        "border": "rgba(15, 76, 129, 0.32)",
        "label": "Требуется допроверка",
    },
}

EXECUTION_TOKENS = {
    "complete": {"accent": "#15803D", "tint": "rgba(21, 128, 61, 0.08)"},
    "partial": {"accent": "#B45309", "tint": "rgba(180, 83, 9, 0.09)"},
    "failed": {"accent": "#B91C1C", "tint": "rgba(185, 28, 28, 0.08)"},
}

SEVERITY_ACCENT = {
    "critical": "#B91C1C",
    "major": "#B45309",
    "minor": "#0F4C81",
}

CSS = """
<style>
:root {
    --rv-accent: var(--primary-color, #0F4C81);
    --rv-border: rgba(100, 116, 139, 0.24);
    --rv-border-strong: rgba(100, 116, 139, 0.42);
    --rv-surface: var(--secondary-background-color);
    --rv-canvas: var(--background-color);
    --rv-text: var(--text-color);
    --rv-muted: color-mix(in srgb, var(--text-color) 66%, transparent);
    --rv-subtle-bg: color-mix(in srgb, var(--text-color) 4%, var(--secondary-background-color));
    --rv-radius: 6px;
}

.block-container {
    padding-top: 2.65rem;
    padding-bottom: 3rem;
    max-width: 1240px;
}

/* Accessible keyboard focus across interactive controls */
button:focus-visible,
input:focus-visible,
textarea:focus-visible,
select:focus-visible,
a:focus-visible,
[role="tab"]:focus-visible,
[role="radio"]:focus-visible,
[role="checkbox"]:focus-visible {
    outline: 2px solid var(--rv-accent) !important;
    outline-offset: 2px !important;
}

/* Compact Enterprise Top Bar */
.rv-topbar {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: 0.75rem 1.25rem;
    padding-bottom: 0.9rem;
    margin-bottom: 0.95rem;
    border-bottom: 1px solid var(--rv-border);
}

.rv-brand-kicker {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--rv-accent);
    margin: 0 0 0.2rem 0;
}

.rv-brand-title,
.stMarkdown h1.rv-brand-title {
    font-size: clamp(1.2rem, 1.7vw, 1.48rem) !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em !important;
    color: var(--rv-text) !important;
    margin: 0 !important;
    padding: 0 !important;
    line-height: 1.25 !important;
}

.rv-brand-desc {
    font-size: 0.87rem;
    color: var(--rv-muted);
    margin: 0.25rem 0 0 0;
    line-height: 1.45;
    max-width: 780px;
}

.rv-standards {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: center;
}

.rv-std-chip {
    font-size: 0.74rem;
    font-weight: 600;
    color: var(--rv-muted);
    border: 1px solid var(--rv-border);
    background: var(--rv-surface);
    border-radius: 4px;
    padding: 0.2rem 0.5rem;
    white-space: nowrap;
}

/* Linear Workflow Rail */
.rv-workflow {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem 0.65rem;
    padding: 0.55rem 0.8rem;
    margin-bottom: 0.95rem;
    background: var(--rv-surface);
    border: 1px solid var(--rv-border);
    border-radius: var(--rv-radius);
    font-size: 0.78rem;
}

.rv-wf-step {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--rv-muted);
    white-space: nowrap;
}

.rv-wf-step--active {
    color: var(--rv-text);
    font-weight: 600;
}

.rv-wf-step--done {
    color: var(--rv-accent);
    font-weight: 500;
}

.rv-wf-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.15rem;
    height: 1.15rem;
    border-radius: 999px;
    font-size: 0.7rem;
    font-weight: 600;
    border: 1px solid var(--rv-border-strong);
}

.rv-wf-step--active .rv-wf-num {
    background: var(--rv-accent);
    border-color: var(--rv-accent);
    color: #FFFFFF;
}

.rv-wf-step--done .rv-wf-num {
    border-color: var(--rv-accent);
    color: var(--rv-accent);
}

.rv-wf-sep {
    color: var(--rv-border-strong);
    user-select: none;
}

/* Section Headings inside Cards */
.rv-section-title {
    font-size: 1.02rem;
    font-weight: 650;
    letter-spacing: -0.01em;
    color: var(--rv-text);
    margin: 0 0 0.2rem 0;
}

.rv-section-sub {
    font-size: 0.84rem;
    color: var(--rv-muted);
    margin: 0 0 0.75rem 0;
    line-height: 1.45;
}

/* Privacy / Limits Specification Strip */
.rv-spec-panel {
    border: 1px solid var(--rv-border);
    border-radius: var(--rv-radius);
    background: var(--rv-subtle-bg);
    padding: 0.8rem 0.95rem;
    font-size: 0.83rem;
    line-height: 1.5;
    color: var(--rv-text);
}

.rv-spec-panel-title {
    font-size: 0.75rem;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--rv-muted);
    margin-bottom: 0.45rem;
}

.rv-spec-row {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.22rem 0;
    border-bottom: 1px solid var(--rv-border);
}

.rv-spec-row:last-child {
    border-bottom: none;
}

.rv-spec-key {
    color: var(--rv-muted);
}

.rv-spec-val {
    font-weight: 600;
    color: var(--rv-text);
    text-align: right;
}

/* Executive Verdict Grid (Readiness vs Execution Completeness separated) */
.rv-exec-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
    gap: 0.85rem;
    margin: 0.45rem 0 0.85rem 0;
}

.rv-verdict-box,
.rv-exec-box {
    border: 1px solid var(--rv-border);
    border-radius: var(--rv-radius);
    padding: 0.95rem 1.1rem;
    background: var(--rv-surface);
}

.rv-verdict-box {
    border-left: 4px solid var(--rv-accent);
}

.rv-exec-box {
    border-left: 4px solid var(--rv-border-strong);
}

.rv-kicker {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 0.25rem;
}

.rv-verdict-value {
    font-size: clamp(1.18rem, 1.6vw, 1.38rem);
    font-weight: 700;
    color: var(--rv-text);
    line-height: 1.25;
}

.rv-exec-value {
    font-size: 1.05rem;
    font-weight: 650;
    color: var(--rv-text);
    line-height: 1.3;
}

.rv-exec-detail {
    font-size: 0.83rem;
    color: var(--rv-muted);
    margin-top: 0.35rem;
    line-height: 1.45;
}

/* Compact Key-Value Passport Bar */
.rv-passport {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
    gap: 0.5rem 0.9rem;
    padding: 0.65rem 0.9rem;
    margin-bottom: 0.85rem;
    border: 1px solid var(--rv-border);
    border-radius: var(--rv-radius);
    background: var(--rv-surface);
}

.rv-pass-item {
    min-width: 0;
}

.rv-pass-label {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--rv-muted);
}

.rv-pass-value {
    font-size: 0.84rem;
    font-weight: 600;
    color: var(--rv-text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* Summary & Remediation Cards */
.rv-card {
    border: 1px solid var(--rv-border);
    border-radius: var(--rv-radius);
    padding: 0.9rem 1rem;
    background: var(--rv-surface);
    color: var(--rv-text);
    margin-bottom: 0.75rem;
}

.rv-card-header {
    font-size: 0.86rem;
    font-weight: 650;
    color: var(--rv-text);
    margin-bottom: 0.55rem;
}

.rv-bar {
    display: flex;
    height: 6px;
    width: 100%;
    border-radius: 3px;
    overflow: hidden;
    background: var(--rv-border);
    margin: 0.35rem 0 0.75rem 0;
}

.rv-breakdown {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 0.45rem 0.75rem;
    font-size: 0.82rem;
}

.rv-break-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.25rem 0;
    border-bottom: 1px solid var(--rv-border);
}

.rv-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 999px;
    margin-right: 0.4rem;
    flex-shrink: 0;
}

.rv-action-list {
    margin: 0;
    padding: 0;
    list-style: none;
}

.rv-action-item {
    display: flex;
    gap: 0.55rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--rv-border);
    font-size: 0.85rem;
    line-height: 1.45;
    color: var(--rv-text);
}

.rv-action-item:last-child {
    border-bottom: none;
    padding-bottom: 0;
}

.rv-action-idx {
    font-weight: 700;
    color: var(--rv-accent);
    min-width: 1.25rem;
}

/* Finding Metadata Line & GitHub-diff-inspired Evidence Header */
.rv-meta-line {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.9rem;
    padding: 0.45rem 0.7rem;
    margin-bottom: 0.65rem;
    background: var(--rv-subtle-bg);
    border: 1px solid var(--rv-border);
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--rv-muted);
}

.rv-meta-line strong {
    color: var(--rv-text);
    font-weight: 600;
}

.rv-evidence-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.35rem 0.75rem;
    padding: 0.35rem 0.65rem;
    margin-top: 0.45rem;
    border: 1px solid var(--rv-border);
    border-bottom: none;
    border-radius: 5px 5px 0 0;
    background: var(--rv-subtle-bg);
    font-size: 0.77rem;
    color: var(--rv-muted);
}

.rv-evidence-loc {
    font-weight: 600;
    color: var(--rv-text);
}

/* Progress Stage Table */
.rv-stage-list {
    display: grid;
    gap: 0.3rem;
    margin-top: 0.45rem;
    font-size: 0.82rem;
}

.rv-stage-row {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.3rem 0.55rem;
    border-radius: 4px;
    border: 1px solid var(--rv-border);
    background: var(--rv-surface);
}

/* Export Footer Strip */
.rv-export-bar {
    border: 1px solid var(--rv-border);
    border-radius: var(--rv-radius);
    background: var(--rv-surface);
    padding: 0.8rem 1rem 0.45rem 1rem;
    margin-top: 0.85rem;
}

/* Metric Cards & Tabs Polish */
div[data-testid="stMetric"] {
    background: var(--rv-surface);
    border-radius: var(--rv-radius);
    padding: 0.7rem 0.9rem;
}

div[data-testid="stTabs"] [role="tablist"] {
    gap: 1.1rem;
    overflow-x: auto;
    overflow-y: hidden;
    flex-wrap: nowrap;
    scrollbar-width: thin;
}

div[data-testid="stTabs"] [role="tab"] {
    white-space: nowrap;
    flex-shrink: 0;
    font-size: 0.86rem;
    font-weight: 500;
    padding-left: 0.15rem;
    padding-right: 0.15rem;
}

/* Prevent horizontal overflow on small screens */
.stApp,
[data-testid="stAppViewContainer"] {
    overflow-x: clip;
}

/* Status Pills for Remediation Proposals */
.rv-pill {
    display: inline-block;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 0.14rem 0.48rem;
    border-radius: 4px;
    border: 1px solid var(--rv-border);
    background: var(--rv-subtle-bg);
    color: var(--rv-text);
    white-space: nowrap;
}

.rv-pill--approved {
    border-color: rgba(21, 128, 61, 0.38);
    background: rgba(21, 128, 61, 0.09);
    color: #15803D;
}

.rv-pill--rejected {
    border-color: rgba(185, 28, 28, 0.35);
    background: rgba(185, 28, 28, 0.08);
    color: #B91C1C;
}

.rv-pill--warn {
    border-color: rgba(180, 83, 9, 0.38);
    background: rgba(180, 83, 9, 0.09);
    color: #B45309;
}

@media (max-width: 768px) {
    .block-container {
        padding-top: 2rem;
        padding-left: 0.9rem;
        padding-right: 0.9rem;
    }
    .rv-exec-grid {
        grid-template-columns: 1fr;
    }
    .rv-workflow {
        font-size: 0.74rem;
        gap: 0.25rem 0.45rem;
    }
    .rv-passport {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

@media (max-width: 480px) {
    .block-container {
        padding-left: 0.65rem;
        padding-right: 0.65rem;
    }
    .rv-topbar {
        padding-bottom: 0.65rem;
        margin-bottom: 0.7rem;
    }
    .rv-passport {
        grid-template-columns: 1fr;
    }
    .rv-spec-row {
        flex-direction: column;
        gap: 0.1rem;
    }
    .rv-spec-val {
        text-align: left;
    }
}
</style>
"""


def inject_theme() -> None:
    """Inject restrained Enterprise Fintech styles compatible with light and dark Streamlit themes."""
    st.markdown(CSS, unsafe_allow_html=True)
