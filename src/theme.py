"""
theme.py — Tema visual do OneImport (paleta, tipografia e estilos de
componentes do Streamlit via CSS injetado).
"""

import streamlit as st


def aplicar_tema_visual() -> None:
    st.markdown("""
        <style>
        /* Fundo principal e tipografia */
        .stApp {
            background-color: #F8FAFC;
            font-family: 'Inter', sans-serif;
        }

        /* Sidebar escuro com texto e elementos adaptados */
        [data-testid="stSidebar"] {
            background-color: #0F172A !important;
        }

        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] span {
            color: #F8FAFC !important;
        }

        /* Ajuste nos inputs e selectboxes da sidebar */
        [data-testid="stSidebar"] div[data-baseweb="select"] > div,
        [data-testid="stSidebar"] input {
            background-color: #1E293B !important;
            color: #FFFFFF !important;
            border-color: #334155 !important;
        }

        /* Ajuste nos uploader de arquivo da sidebar */
        [data-testid="stSidebar"] [data-testid="stFileUploader"] {
            background-color: #1E293B !important;
            border: 1px dashed #334155 !important;
            border-radius: 8px;
            padding: 10px;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploader"] section {
            background-color: #1E293B !important;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploader"] button {
            background-color: #2563EB !important;
            color: #FFFFFF !important;
        }

        /* O menu suspenso do selectbox é renderizado fora da sidebar (portal no
           final do <body>) — precisa de uma regra própria para o texto das
           opções ficar legível (fundo claro padrão, texto escuro). */
        [data-baseweb="popover"] li,
        [data-baseweb="menu"] li {
            color: #0F172A !important;
        }

        /* Botões Principais */
        .stButton>button {
            background-color: #2563EB;
            color: white !important;
            border-radius: 8px;
            border: none;
            padding: 0.5rem 1rem;
            font-weight: 600;
            transition: all 0.2s ease;
        }
        .stButton>button:hover {
            background-color: #1D4ED8;
            box-shadow: 0 4px 6px -1px rgba(37, 99, 235, 0.2);
        }

        /* Cards e Métricas */
        [data-testid="stMetricValue"] {
            font-size: 1.8rem;
            font-weight: 700;
            color: #0F172A;
        }

        /* Cabeçalhos */
        h1, h2, h3 {
            color: #0F172A;
            font-weight: 700;
            letter-spacing: -0.02em;
        }
        </style>
    """, unsafe_allow_html=True)
