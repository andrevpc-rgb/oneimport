"""
app.py — Interface Streamlit do OneImport.

Fluxo: upload do extrato (+ opcionalmente Plano de Contas e Razão anterior)
-> processamento (main.processar_pipeline, que chama parser.py e
classifier.py) -> painel de revisão dos lançamentos pendentes -> exportação
dirigida por um layout dinâmico (exporter.py + database.py — motor
multi-ERP). Duas abas dão acesso direto às regras e aos layouts de
exportação, independente de estar processando um extrato no momento.

Persistência (regras, empresas, layouts) em Postgres/Supabase, multi-tenant
por login (cada escritório cadastrado é um tenant isolado — ver
database.UsuariosDB e a tela de login/cadastro logo abaixo). Requer só
DATABASE_URL em src/.streamlit/secrets.toml (local — veja
secrets.toml.example) ou como variável de ambiente (deploy, ex. Render).

Execução: streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import classifier
import database
import exporter
import main as orquestrador
import parser as extrator
import theme

NOME_APP = "OneImport"
_CAMINHO_LOGO = Path(__file__).parent / "assets" / "logo_oneimport.svg"

st.set_page_config(page_title=NOME_APP, page_icon=str(_CAMINHO_LOGO), layout="wide")
theme.aplicar_tema_visual()


def _logo_svg(tamanho: int = 32) -> str:
    """Marcação SVG inline do logo (mesmo desenho de assets/logo_oneimport.svg), para colocar ao lado do nome."""
    return (
        f'<svg width="{tamanho}" height="{tamanho}" viewBox="0 0 32 32" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Logo OneImport">'
        '<rect width="32" height="32" rx="8" fill="#2563eb"/>'
        '<path d="M16 7v13m0 0-5-5m5 5 5-5" stroke="#ffffff" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
        '<rect x="8" y="23" width="16" height="2.5" rx="1.25" fill="#ffffff"/>'
        "</svg>"
    )


_CORES_STATUS = {
    classifier.STATUS_REGRA: "background-color: #d4edda",
    classifier.STATUS_RAZAO_ANTERIOR: "background-color: #fff3cd",
    classifier.STATUS_REVISAR: "background-color: #f8d7da",
    classifier.STATUS_IGNORADO: "background-color: #e2e3e5; color: #6c757d",
    classifier.STATUS_MANUAL: "background-color: #d1ecf1",
    classifier.STATUS_PROPAGADO: "background-color: #cfe2ff",
}

_COLUNAS_TABELA = [
    "Data",
    "Histórico bancário (original)",
    "Tipo lançamento",
    "Valor",
    "Conta débito",
    "Conta crédito",
    "Número histórico",
    "Variável",
    "Status classificação",
    "Confiança (%)",
]

_COLUNAS_REGRA = (
    "cnpj_empresa", "codigo_banco", "padrao_texto", "tipo_padrao",
    "conta_debito", "conta_credito", "numero_historico", "formato_variavel",
)

_OPCAO_NENHUMA_EMPRESA = "— Nenhuma (CNPJ livre) —"
_OPCAO_NOVA_EMPRESA = "+ Cadastrar nova empresa..."


def _cor_status(valor: str) -> str:
    return _CORES_STATUS.get(valor, "")


def _codigo_da_opcao(opcao: str) -> str:
    """Extrai o código de uma opção no formato 'descrição — código'."""
    return opcao.rsplit(" — ", 1)[-1].strip() if opcao else ""


def _indice_da_conta(opcoes: list, codigo) -> int:
    """Localiza, em `opcoes` (['' ] + lista 'descrição — código'), a opção cujo código é `codigo`."""
    codigo = str(codigo).strip()
    if not codigo:
        return 0
    for pos, opcao in enumerate(opcoes):
        if opcao and _codigo_da_opcao(opcao) == codigo:
            return pos
    return 0


def _opcoes_contas_analiticas(plano_de_contas) -> list:
    """Lista 'descrição — código' das contas analíticas de um PlanoDeContas, ordenada pelo nome."""
    if plano_de_contas is None or not plano_de_contas.col_descricao:
        return []
    col_codigo = plano_de_contas.col_codigo_estruturado or plano_de_contas.col_codigo_reduzido
    opcoes = [
        f"{linha[plano_de_contas.col_descricao]} — {linha[col_codigo]}"
        for _, linha in plano_de_contas.df.iterrows()
        if plano_de_contas.eh_analitica(linha[col_codigo])
    ]
    opcoes.sort()
    return opcoes


@st.cache_data(show_spinner=False)
def _carregar_plano_de_contas_preview(arquivo_upload):
    """
    Lê o Plano de Contas assim que é enviado (antes de 'Processar extrato'),
    só para alimentar o seletor da conta do Banco — direto do objeto em
    memória do upload, sem gravar nada em disco (Zero Data Retention). O
    cache do Streamlit também é só em memória por padrão (sem persist=True).
    """
    return classifier.ler_plano_de_contas(arquivo_upload)


# --------------------------------------------------------------------------- #
# Cache das consultas ao Postgres (Supabase)
#
# O Streamlit reexecuta o script inteiro a cada interação (escolher uma
# conta, marcar um checkbox...) — sem cache, isso significa refazer as
# consultas de Empresas/Regras/Layouts ao Supabase a cada clique em
# QUALQUER lugar da tela, mesmo sem nada ter mudado ali. Essas funções
# guardam o resultado por alguns segundos; _limpar_caches_bd() é chamada
# logo após qualquer escrita (cadastrar/editar/excluir), pra não mostrar
# dado desatualizado.
# --------------------------------------------------------------------------- #

@st.cache_data(ttl=30, show_spinner=False)
def _cache_listar_empresas() -> pd.DataFrame:
    with database.BancoDados() as db:
        return db.listar_empresas()


@st.cache_data(ttl=30, show_spinner=False)
def _cache_listar_layouts() -> pd.DataFrame:
    with database.BancoDados() as db:
        return db.listar_layouts()


@st.cache_data(ttl=30, show_spinner=False)
def _cache_obter_layout(id_layout: int):
    with database.BancoDados() as db:
        return db.obter_layout(id_layout)


@st.cache_data(ttl=30, show_spinner=False)
def _cache_obter_layout_por_nome(nome_layout: str):
    with database.BancoDados() as db:
        return db.obter_layout_por_nome(nome_layout)


@st.cache_data(ttl=30, show_spinner=False)
def _cache_listar_regras(cnpj_empresa: str) -> pd.DataFrame:
    with database.RegrasDB() as db:
        return db.listar_regras(cnpj_empresa or None)


def _limpar_caches_bd() -> None:
    """Invalida os caches de leitura ao banco — chame antes do st.rerun() que segue qualquer escrita."""
    _cache_listar_empresas.clear()
    _cache_listar_layouts.clear()
    _cache_obter_layout.clear()
    _cache_obter_layout_por_nome.clear()
    _cache_listar_regras.clear()


def _renderizar_gerenciador_regras(cnpj_ativo: str = "") -> None:
    """Consultar, incluir, editar e excluir regras (tabela regras_de_para, Postgres), independente do fluxo de processamento."""
    st.header("Gerenciador de Regras")
    st.caption("Regras de classificação — isoladas por tenant no Postgres/Supabase (ver database.py).")

    # a key inclui o CNPJ ativo de propósito: quando a Empresa muda na barra lateral, o Streamlit
    # trata isso como um widget novo e reaplica `value=cnpj_ativo`, mesmo que o usuário tivesse
    # digitado outro filtro manualmente antes — é o que faz o campo "atualizar dinamicamente".
    filtro_cnpj = st.text_input(
        "Filtrar por CNPJ (pré-preenchido com a empresa ativa na barra lateral — deixe em branco para ver todas)",
        value=cnpj_ativo,
        key=f"filtro_cnpj_regras_{cnpj_ativo or 'todas'}",
    )

    if cnpj_ativo:
        if st.button(
            f"Atribuir CNPJ {cnpj_ativo} a todas as regras sem CNPJ (coringa)",
            key=f"atribuir_cnpj_massa_{cnpj_ativo}",
            help="Útil para regras salvas na revisão antes de escolher a Empresa na barra lateral.",
        ):
            try:
                with database.RegrasDB() as regras_db:
                    quantidade = regras_db.atribuir_cnpj_em_massa(cnpj_ativo)
                st.success(f"{quantidade} regra(s) atualizada(s) com o CNPJ {cnpj_ativo}.")
                _limpar_caches_bd()
                st.rerun()
            except database.RegraInvalidaError as exc:
                st.error(str(exc))

    try:
        df_regras = _cache_listar_regras(filtro_cnpj.strip())
    except database.ConfiguracaoAusenteError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.error(f"Não foi possível consultar as regras: {exc}")
        return

    st.subheader(f"Regras cadastradas ({len(df_regras)})")
    if df_regras.empty:
        st.info("Nenhuma regra cadastrada ainda — use o formulário abaixo para incluir a primeira.")
    else:
        df_editor = df_regras[["id", *_COLUNAS_REGRA]].copy()
        df_editor.insert(1, "Excluir", False)

        df_editado = st.data_editor(
            df_editor,
            key="editor_regras",
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            disabled=["id"],
            column_config={
                "Excluir": st.column_config.CheckboxColumn(
                    help="Marque e clique em 'Salvar alterações' para excluir esta regra."
                ),
                "tipo_padrao": st.column_config.SelectboxColumn(options=["contém", "regex"]),
                "formato_variavel": st.column_config.TextColumn(
                    help="Use {descricao} para inserir a descrição do lançamento na hora de classificar."
                ),
            },
        )

        if st.button("Salvar alterações", key="salvar_edicoes_regras"):
            linhas_excluir = df_editado[df_editado["Excluir"]]
            linhas_manter = df_editado[~df_editado["Excluir"]]

            try:
                with database.RegrasDB() as regras_db:
                    for id_regra in linhas_excluir["id"]:
                        regras_db.remover_regra(int(id_regra))

                    avisos = []
                    for _, linha in linhas_manter.iterrows():
                        padrao = str(linha["padrao_texto"]).strip() if pd.notna(linha["padrao_texto"]) else ""
                        if not padrao:
                            avisos.append(f"Regra #{int(linha['id'])}: padrão de texto vazio — não foi salva.")
                            continue
                        regras_db.atualizar_regra(
                            int(linha["id"]),
                            cnpj_empresa=(str(linha["cnpj_empresa"]).strip() or None) if pd.notna(linha["cnpj_empresa"]) else None,
                            codigo_banco=(str(linha["codigo_banco"]).strip() or None) if pd.notna(linha["codigo_banco"]) else None,
                            padrao_texto=padrao,
                            tipo_padrao=linha["tipo_padrao"] or "contém",
                            conta_debito=str(linha["conta_debito"]).strip() if pd.notna(linha["conta_debito"]) else "",
                            conta_credito=str(linha["conta_credito"]).strip() if pd.notna(linha["conta_credito"]) else "",
                            numero_historico=str(linha["numero_historico"]).strip() if pd.notna(linha["numero_historico"]) else "",
                            formato_variavel=str(linha["formato_variavel"]).strip() if pd.notna(linha["formato_variavel"]) else "",
                        )
                if avisos:
                    st.warning("\n".join(avisos))
                st.success(f"{len(linhas_excluir)} regra(s) excluída(s); demais alterações salvas.")
                _limpar_caches_bd()
                st.rerun()
            except database.RegraInvalidaError as exc:
                st.error(str(exc))

    st.divider()
    st.subheader("Incluir nova regra")
    with st.form("form_nova_regra", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            novo_cnpj = st.text_input(
                "CNPJ da empresa (obrigatório)",
                value=cnpj_ativo,
                help="Toda regra precisa estar vinculada a uma empresa — evita 'coringas' criados sem querer. "
                "Pré-preenchido com a empresa ativa na barra lateral.",
            )
            novo_banco = st.text_input("Código da conta do Banco (opcional — vazio vale para qualquer conta)")
            novo_padrao = st.text_input("Padrão de texto (obrigatório)")
            novo_tipo_padrao = st.selectbox("Tipo de padrão", options=["contém", "regex"])
        with col2:
            nova_conta_debito = st.text_input("Conta débito")
            nova_conta_credito = st.text_input("Conta crédito")
            novo_numero_historico = st.text_input("Número histórico")
            novo_formato_variavel = st.text_input(
                "Variável (use {descricao} para a descrição do lançamento)",
                value=classifier.MASCARA_VARIAVEL_PADRAO,
            )

        enviado = st.form_submit_button("Adicionar regra", type="primary")
        if enviado:
            if not novo_cnpj.strip():
                st.warning("Informe o CNPJ da empresa (selecione uma Empresa na barra lateral, se preferir).")
            elif not novo_padrao.strip():
                st.warning("Informe o padrão de texto.")
            elif not nova_conta_debito.strip() or not nova_conta_credito.strip():
                st.warning("Informe a conta débito e a conta crédito.")
            else:
                try:
                    with database.RegrasDB() as regras_db:
                        regras_db.adicionar_regra(
                            padrao_texto=novo_padrao.strip(),
                            conta_debito=nova_conta_debito.strip(),
                            conta_credito=nova_conta_credito.strip(),
                            numero_historico=novo_numero_historico.strip(),
                            formato_variavel=novo_formato_variavel.strip(),
                            cnpj_empresa=novo_cnpj.strip() or None,
                            codigo_banco=novo_banco.strip() or None,
                            tipo_padrao=novo_tipo_padrao,
                        )
                    st.success("Regra adicionada.")
                    _limpar_caches_bd()
                    st.rerun()
                except database.RegraInvalidaError as exc:
                    st.error(str(exc))


def _renderizar_gerenciador_layouts() -> None:
    """Consultar, incluir, editar e excluir layouts de exportação (tabela layouts_exportacao, Postgres) — Motor de Layouts Dinâmicos."""
    st.header("Gerenciador de Layouts de Exportação")
    st.caption(
        f"O layout **{database.NOME_LAYOUT_PADRAO}** é global do sistema (visível para todos os tenants) "
        "e não pode ser excluído, mas pode ser editado. Os demais são próprios do seu tenant."
    )

    try:
        df_layouts = _cache_listar_layouts()
    except database.ConfiguracaoAusenteError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.error(f"Não foi possível consultar os layouts: {exc}")
        return

    if df_layouts.empty:
        st.info("Nenhum layout cadastrado.")
        return

    opcoes_layout = [f"{linha['nome_layout']} — #{linha['id']}" for _, linha in df_layouts.iterrows()]
    selecao = st.selectbox("Layout para consultar/editar", options=opcoes_layout, key="layout_selecionado_gerenciador")
    id_layout = int(selecao.rsplit("#", 1)[-1])

    layout = _cache_obter_layout(id_layout)

    st.subheader(f"Editando: {layout.nome_layout}")
    eh_padrao = layout.nome_layout == database.NOME_LAYOUT_PADRAO

    col1, col2, col3 = st.columns(3)
    with col1:
        novo_nome = st.text_input("Nome do layout", value=layout.nome_layout, key=f"nome_layout_{id_layout}")
    with col2:
        formatos = ["xlsx", "csv", "txt"]
        novo_formato = st.selectbox(
            "Formato do arquivo", options=formatos, index=formatos.index(layout.formato_arquivo),
            key=f"formato_layout_{id_layout}",
        )
    with col3:
        novo_delimitador = st.text_input(
            "Separador (csv/txt)", value=layout.delimitador, key=f"delim_layout_{id_layout}",
            disabled=novo_formato == "xlsx",
        )
    novo_formato_data = st.text_input(
        "Formato da data (padrão strftime — ex.: %d/%m/%Y, %Y-%m-%d, %d-%m-%Y)",
        value=layout.formato_data,
        key=f"data_layout_{id_layout}",
    )

    st.markdown(
        "**Mapeamento de colunas** — ordem, nome da coluna no arquivo final e de qual coluna do sistema ela vem. "
        "Adicione/remova linhas pelos botões da própria tabela."
    )
    df_mapa = pd.DataFrame(layout.mapeamento_colunas or [], columns=["coluna_saida", "coluna_sistema"])
    df_mapa_editado = st.data_editor(
        df_mapa,
        key=f"editor_mapa_{id_layout}",
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "coluna_saida": st.column_config.TextColumn("Coluna no arquivo final", required=True),
            "coluna_sistema": st.column_config.SelectboxColumn(
                "Coluna do sistema", options=database.COLUNAS_SISTEMA_DISPONIVEIS, required=True
            ),
        },
    )

    col_salvar, col_excluir = st.columns(2)
    with col_salvar:
        if st.button("Salvar layout", key=f"salvar_layout_{id_layout}", type="primary"):
            mapeamento = []
            for registro in df_mapa_editado.to_dict("records"):
                coluna_sistema = str(registro.get("coluna_sistema") or "").strip()
                if not coluna_sistema or coluna_sistema == "nan":
                    continue
                coluna_saida = str(registro.get("coluna_saida") or "").strip() or coluna_sistema
                mapeamento.append({"coluna_saida": coluna_saida, "coluna_sistema": coluna_sistema})

            try:
                with database.BancoDados() as db:
                    db.atualizar_layout(
                        id_layout,
                        nome_layout=novo_nome.strip(),
                        formato_arquivo=novo_formato,
                        delimitador=novo_delimitador,
                        formato_data=novo_formato_data.strip(),
                        mapeamento_colunas=mapeamento,
                    )
                st.success("Layout salvo.")
                _limpar_caches_bd()
                st.rerun()
            except database.LayoutInvalidoError as exc:
                st.error(str(exc))
    with col_excluir:
        if st.button("Excluir este layout", key=f"excluir_layout_{id_layout}", disabled=eh_padrao):
            try:
                with database.BancoDados() as db:
                    db.remover_layout(id_layout)
                st.success("Layout excluído.")
                _limpar_caches_bd()
                st.rerun()
            except database.LayoutInvalidoError as exc:
                st.error(str(exc))

    st.divider()
    st.subheader("Incluir novo layout")
    with st.form("form_novo_layout", clear_on_submit=True):
        nome_novo = st.text_input("Nome do novo layout (ex.: 'Domínio Sistemas', 'Alterdata')")
        formato_novo = st.selectbox("Formato do arquivo", options=["xlsx", "csv", "txt"], key="formato_novo_layout")
        delimitador_novo = st.text_input("Separador (csv/txt)", value=";", key="delim_novo_layout")
        formato_data_novo = st.text_input("Formato da data", value="%d/%m/%Y", key="data_novo_layout")
        copiar_calima = st.checkbox(
            f"Começar copiando o mapeamento de colunas do '{database.NOME_LAYOUT_PADRAO}'", value=True
        )
        criar = st.form_submit_button("Criar layout", type="primary")
        if criar:
            if not nome_novo.strip():
                st.warning("Informe o nome do layout.")
            else:
                mapeamento_inicial = (
                    [{"coluna_saida": c, "coluna_sistema": c} for c in extrator.COLUNAS_CALIMA]
                    if copiar_calima
                    else [{"coluna_saida": "Data", "coluna_sistema": "Data"}]
                )
                try:
                    with database.BancoDados() as db:
                        db.adicionar_layout(
                            nome_layout=nome_novo.strip(),
                            formato_arquivo=formato_novo,
                            mapeamento_colunas=mapeamento_inicial,
                            delimitador=delimitador_novo,
                            formato_data=formato_data_novo.strip(),
                        )
                    st.success(f"Layout '{nome_novo.strip()}' criado — selecione-o acima para ajustar o mapeamento.")
                    _limpar_caches_bd()
                    st.rerun()
                except database.LayoutInvalidoError as exc:
                    st.error(str(exc))


def _inicializar_estado() -> None:
    padroes = {
        "df_classificado": None,
        "plano_de_contas": None,
        "inconsistencias_plano": pd.DataFrame(),
        "conta_banco": "",
        "cnpj_empresa": "",
        "nome_layout_selecionado": database.NOME_LAYOUT_PADRAO,
    }
    for chave, valor in padroes.items():
        if chave not in st.session_state:
            st.session_state[chave] = valor


# prefixos das keys de widget por linha do painel de revisão (ver o loop de pendências
# mais abaixo) — todas amarradas ao índice 0, 1, 2... do DataFrame do extrato atual.
_PREFIXOS_ESTADO_LINHA = (
    "debito_", "credito_", "num_hist_", "variavel_", "salvar_regra_", "padrao_", "aplicar_", "ignorar_",
)


def _limpar_dados_extrato() -> None:
    """
    Remove do session_state qualquer dado do extrato anterior: o DataFrame
    classificado, o Plano de Contas carregado, as inconsistências, e também
    o estado de cada widget de revisão por linha (contas/variável/histórico
    digitados) — sem isso, eles ficariam associados por engano às linhas do
    próximo extrato (mesmos índices 0, 1, 2...). Chamada ao trocar de
    Empresa na barra lateral e antes de processar um novo extrato: política
    de retenção zero — nenhum dado bancário sobrevive além do necessário
    para a sessão ativa, e nada disso é gravado em disco.
    """
    st.session_state["df_classificado"] = None
    st.session_state["plano_de_contas"] = None
    st.session_state["inconsistencias_plano"] = pd.DataFrame()

    for chave in list(st.session_state.keys()):
        if chave.startswith(_PREFIXOS_ESTADO_LINHA):
            del st.session_state[chave]


def _tela_login() -> None:
    """
    Tela de login/cadastro de escritório — é tudo que aparece enquanto
    st.session_state['tenant_id'] não está definido. Cada escritório
    cadastrado aqui é o próprio tenant (isolamento multi-tenant real,
    substitui o TENANT_ID fixo usado nos testes locais).
    """
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:14px;margin-bottom:0.25rem;">'
        f'{_logo_svg(40)}<h1 style="margin:0;padding:0;">{NOME_APP}</h1></div>',
        unsafe_allow_html=True,
    )
    st.caption("Extrai, classifica e exporta extratos bancários — layout de saída configurável por empresa/ERP.")

    if not database.esta_configurado():
        st.error(
            "Banco de dados não configurado. Defina DATABASE_URL em src/.streamlit/secrets.toml "
            "(veja secrets.toml.example) ou como variável de ambiente antes de fazer login."
        )
        return

    aba_entrar, aba_cadastrar = st.tabs(["Entrar", "Cadastrar novo escritório"])

    with aba_entrar:
        with st.form("form_login"):
            email_login = st.text_input("E-mail")
            senha_login = st.text_input("Senha", type="password")
            entrar = st.form_submit_button("Entrar", type="primary")
            if entrar:
                try:
                    usuario = database.UsuariosDB().autenticar(email_login, senha_login)
                    st.session_state["tenant_id"] = usuario["id"]
                    st.session_state["usuario_email"] = usuario["email"]
                    st.session_state["nome_escritorio"] = usuario["nome_escritorio"]
                    st.rerun()
                except (database.UsuarioInvalidoError, database.ConfiguracaoAusenteError) as exc:
                    st.error(str(exc))

    with aba_cadastrar:
        st.caption("Cria um novo escritório contábil — seus dados (empresas, regras, layouts) ficam isolados dos demais.")
        with st.form("form_cadastro_escritorio"):
            nome_escritorio_novo = st.text_input("Nome do escritório")
            email_novo = st.text_input("E-mail", key="email_cadastro")
            senha_nova = st.text_input("Senha (mínimo 6 caracteres)", type="password", key="senha_cadastro")
            senha_confirmar = st.text_input("Confirme a senha", type="password", key="senha_cadastro_confirmar")
            cadastrar = st.form_submit_button("Criar escritório", type="primary")
            if cadastrar:
                if senha_nova != senha_confirmar:
                    st.error("As senhas não coincidem.")
                else:
                    try:
                        usuario = database.UsuariosDB().cadastrar(email_novo, senha_nova, nome_escritorio_novo)
                        st.session_state["tenant_id"] = usuario["id"]
                        st.session_state["usuario_email"] = usuario["email"]
                        st.session_state["nome_escritorio"] = usuario["nome_escritorio"]
                        st.success(f"Escritório '{usuario['nome_escritorio']}' criado!")
                        st.rerun()
                    except (database.UsuarioInvalidoError, database.ConfiguracaoAusenteError) as exc:
                        st.error(str(exc))


_inicializar_estado()

if not st.session_state.get("tenant_id"):
    _tela_login()
    st.stop()

_cnpj_empresa_anterior = st.session_state["cnpj_empresa"]

st.markdown(
    f'<div style="display:flex;align-items:center;gap:14px;margin-bottom:0.25rem;">'
    f'{_logo_svg(40)}<h1 style="margin:0;padding:0;">{NOME_APP}</h1></div>',
    unsafe_allow_html=True,
)
st.caption("Extrai, classifica e exporta extratos bancários — layout de saída configurável por empresa/ERP.")

with st.sidebar:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:0.5rem;">'
        f'{_logo_svg(24)}<strong style="font-size:1.1rem;color:#F8FAFC;">{NOME_APP}</strong></div>',
        unsafe_allow_html=True,
    )

    col_escritorio, col_senha, col_sair = st.columns([3, 1, 1])
    with col_escritorio:
        st.caption(f"🏢 {st.session_state.get('nome_escritorio', '')}")
        st.caption(st.session_state.get("usuario_email", ""))
    with col_senha:
        with st.popover("🔑", help="Trocar senha"):
            with st.form("form_trocar_senha", clear_on_submit=True):
                senha_atual_troca = st.text_input("Senha atual", type="password")
                senha_nova_troca = st.text_input("Nova senha", type="password")
                senha_nova_confirma_troca = st.text_input("Confirmar nova senha", type="password")
                if st.form_submit_button("Salvar nova senha"):
                    if senha_nova_troca != senha_nova_confirma_troca:
                        st.error("As senhas não coincidem.")
                    else:
                        try:
                            database.UsuariosDB().trocar_senha(
                                st.session_state["tenant_id"], senha_atual_troca, senha_nova_troca
                            )
                            st.success("Senha atualizada!")
                        except (database.UsuarioInvalidoError, database.ConfiguracaoAusenteError) as exc:
                            st.error(str(exc))
    with col_sair:
        if st.button("Sair", key="botao_logout"):
            for chave in ("tenant_id", "usuario_email", "nome_escritorio"):
                st.session_state.pop(chave, None)
            _limpar_dados_extrato()
            st.rerun()

    st.header("1. Arquivos de entrada")
    arquivo_extrato = st.file_uploader("Extrato bancário (obrigatório)", type=["pdf", "xlsx", "xls"])
    arquivo_plano_contas = st.file_uploader("Plano de Contas (opcional)", type=["xlsx", "xls", "csv"])
    arquivo_razao = st.file_uploader("Razão contábil anterior (opcional)", type=["pdf", "xlsx", "xls"])

    st.header("2. Parâmetros")

    # --- Empresa ---------------------------------------------------------- #
    try:
        df_empresas = _cache_listar_empresas()
    except Exception:
        df_empresas = pd.DataFrame(columns=["id", "cnpj", "razao_social", "codigo_sistema"])

    opcoes_empresa_nomes = [f"{linha['razao_social']} — {linha['cnpj']}" for _, linha in df_empresas.iterrows()]
    opcoes_empresa_completas = [_OPCAO_NENHUMA_EMPRESA] + opcoes_empresa_nomes + [_OPCAO_NOVA_EMPRESA]

    indice_empresa_atual = 0
    cnpj_salvo = st.session_state["cnpj_empresa"]
    if cnpj_salvo:
        for pos, opcao in enumerate(opcoes_empresa_completas):
            if opcao not in (_OPCAO_NENHUMA_EMPRESA, _OPCAO_NOVA_EMPRESA) and _codigo_da_opcao(opcao) == cnpj_salvo:
                indice_empresa_atual = pos
                break

    selecao_empresa = st.selectbox(
        "Empresa", options=opcoes_empresa_completas, index=indice_empresa_atual, key="selecao_empresa_sidebar"
    )

    if selecao_empresa == _OPCAO_NOVA_EMPRESA:
        with st.expander("Cadastrar nova empresa", expanded=True):
            novo_cnpj_empresa = st.text_input("CNPJ", key="novo_cnpj_empresa")
            nova_razao_social = st.text_input("Razão social", key="nova_razao_empresa")
            novo_codigo_sistema = st.text_input("Código no sistema contábil (opcional)", key="novo_codigo_sistema_empresa")
            if st.button("Salvar empresa", key="salvar_nova_empresa"):
                try:
                    with database.BancoDados() as db_sidebar:
                        db_sidebar.adicionar_empresa(novo_cnpj_empresa, nova_razao_social, novo_codigo_sistema)
                    st.success("Empresa cadastrada — selecione-a na lista acima.")
                    _limpar_caches_bd()
                    st.rerun()
                except database.EmpresaInvalidaError as exc:
                    st.error(str(exc))
        cnpj_empresa = ""
    elif selecao_empresa == _OPCAO_NENHUMA_EMPRESA:
        cnpj_empresa = ""
    else:
        cnpj_empresa = _codigo_da_opcao(selecao_empresa)

    # --- Layout de exportação ---------------------------------------------- #
    try:
        df_layouts_sidebar = _cache_listar_layouts()
    except Exception:
        df_layouts_sidebar = pd.DataFrame(columns=["nome_layout"])

    opcoes_layout_nomes = list(df_layouts_sidebar["nome_layout"]) or [database.NOME_LAYOUT_PADRAO]
    valor_layout_salvo = st.session_state["nome_layout_selecionado"]
    indice_layout_atual = (
        opcoes_layout_nomes.index(valor_layout_salvo)
        if valor_layout_salvo in opcoes_layout_nomes
        else (opcoes_layout_nomes.index(database.NOME_LAYOUT_PADRAO) if database.NOME_LAYOUT_PADRAO in opcoes_layout_nomes else 0)
    )
    nome_layout_selecionado = st.selectbox(
        "Layout de exportação",
        options=opcoes_layout_nomes,
        index=indice_layout_atual,
        help="Define o formato final do arquivo. Cadastre ou edite layouts na aba 'Gerenciador de Layouts'.",
    )

    # --- Conta do Banco ------------------------------------------------------ #
    plano_de_contas_preview = None
    if arquivo_plano_contas is not None:
        try:
            plano_de_contas_preview = _carregar_plano_de_contas_preview(arquivo_plano_contas)
        except Exception:
            plano_de_contas_preview = None  # tela de erro completa aparece ao processar; aqui só cai pro texto livre

    opcoes_banco = _opcoes_contas_analiticas(plano_de_contas_preview)
    if opcoes_banco:
        # mesma fonte de códigos usada no painel de revisão (Código Estruturado, se houver, senão
        # Reduzido) — garante que o valor escolhido aqui sempre bate com as opções do dropdown de
        # contrapartida mais adiante, em vez de depender do usuário digitar o código certo de cabeça.
        opcoes_com_vazio = [""] + opcoes_banco
        selecao_banco = st.selectbox(
            "Código da conta contábil do Banco",
            options=opcoes_com_vazio,
            index=_indice_da_conta(opcoes_com_vazio, st.session_state["conta_banco"]),
            help="A conta analítica que representa esta conta bancária no seu Plano de Contas.",
        )
        conta_banco = _codigo_da_opcao(selecao_banco)
    else:
        conta_banco = st.text_input(
            "Código da conta contábil do Banco",
            value=st.session_state["conta_banco"],
            help="A conta que representa esta conta bancária no seu Plano de Contas. "
            "Envie o Plano de Contas acima para escolher de uma lista em vez de digitar.",
        )

    limiar_confianca = st.slider("Confiança mínima para aceitar por similaridade (%)", 50, 100, 80)

    processar = st.button(
        "Processar extrato", type="primary", disabled=arquivo_extrato is None or not conta_banco.strip()
    )

st.session_state["conta_banco"] = conta_banco
st.session_state["cnpj_empresa"] = cnpj_empresa
st.session_state["nome_layout_selecionado"] = nome_layout_selecionado

if cnpj_empresa != _cnpj_empresa_anterior:
    # empresa trocada na barra lateral — descarta os dados do extrato da empresa anterior
    _limpar_dados_extrato()

tab_importador, tab_regras, tab_layouts = st.tabs(
    ["📥 Importador", "🗂️ Gerenciador de Regras", "🧩 Gerenciador de Layouts"]
)

with tab_importador:
    if processar:
        # antes de processar um extrato novo, apaga qualquer resquício do anterior da sessão
        # (política de retenção zero — nunca fica dado bancário velho pairando se o processamento falhar).
        _limpar_dados_extrato()
        with st.spinner("Extraindo e classificando os lançamentos..."):
            try:
                # os uploads são passados direto da memória (objetos do st.file_uploader) — em
                # nenhum momento o extrato, o Plano de Contas ou a Razão anterior tocam o disco.
                resultado = orquestrador.processar_pipeline(
                    caminho_extrato=arquivo_extrato,
                    conta_banco=conta_banco.strip(),
                    caminho_razao_anterior=arquivo_razao,
                    caminho_plano_contas=arquivo_plano_contas,
                    cnpj_empresa=cnpj_empresa.strip() or None,
                    limiar_confianca=float(limiar_confianca),
                )
                resultado["regras_db"].fechar()

                st.session_state["df_classificado"] = resultado["classificado"]
                st.session_state["plano_de_contas"] = resultado["plano_de_contas"]
                st.session_state["inconsistencias_plano"] = resultado["inconsistencias_plano"]
                st.success(f"{len(resultado['classificado'])} lançamento(s) processado(s).")
            except Exception as exc:  # boundary da interface: qualquer falha vira mensagem, nunca um traceback cru
                st.error(f"Falha ao processar o extrato: {exc}")

    df_classificado = st.session_state.get("df_classificado")

    if df_classificado is not None:
        st.header("Painel de revisão")

        total = len(df_classificado)
        mascara_pendentes = df_classificado["Status classificação"] == classifier.STATUS_REVISAR
        mascara_ignorados = df_classificado["Status classificação"] == classifier.STATUS_IGNORADO
        n_pendentes = int(mascara_pendentes.sum())
        n_ignorados = int(mascara_ignorados.sum())

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total de lançamentos", total)
        col2.metric("Classificados automaticamente", total - n_pendentes - n_ignorados)
        col3.metric("Ignorados", n_ignorados)
        col4.metric("Pendentes de revisão", n_pendentes)

        tabela_estilizada = df_classificado[_COLUNAS_TABELA].style
        if hasattr(tabela_estilizada, "map"):  # pandas >= 2.1 (Styler.applymap foi removido)
            tabela_estilizada = tabela_estilizada.map(_cor_status, subset=["Status classificação"])
        else:  # pandas < 2.1
            tabela_estilizada = tabela_estilizada.applymap(_cor_status, subset=["Status classificação"])

        st.dataframe(tabela_estilizada, use_container_width=True, height=400)

        if n_pendentes:
            st.subheader(f"Lançamentos pendentes de revisão ({n_pendentes})")
            st.caption(
                "Selecione as contas e clique em 'Aplicar' para cada lançamento. Lançamentos com a mesma "
                "descrição (ou que contenham o padrão da regra) são atualizados juntos automaticamente. "
                "Marque a caixa para transformar a associação em regra permanente."
            )

            plano_de_contas = st.session_state.get("plano_de_contas")
            # só contas analíticas (que aceitam lançamento) entram na lista — contas sintéticas
            # (grupos/totalizadores, ex. "CAIXA GERAL — 1.1.1.01") ficam de fora para evitar que
            # alguém lance sem querer numa conta de agrupamento. Mesma fonte de códigos usada no
            # seletor da conta do Banco na barra lateral, então a pré-seleção abaixo sempre bate.
            opcoes_conta = _opcoes_contas_analiticas(plano_de_contas)
            if opcoes_conta:
                st.caption(f"Mostrando apenas as {len(opcoes_conta)} contas analíticas do Plano de Contas — digite para buscar pelo nome.")

            for indice in df_classificado[mascara_pendentes].index:
                linha = df_classificado.loc[indice]
                titulo = (
                    f"Linha {indice} — {linha['Data']} — {linha['Histórico bancário (original)']} "
                    f"— R$ {float(linha['Valor']):.2f} ({linha['Tipo lançamento']})"
                )
                # o classificador já preenche o lado do Banco automaticamente (conta_banco +
                # tipo do lançamento): o valor já vem pronto em 'Conta débito'/'Conta crédito',
                # o usuário só precisa escolher a contrapartida que ainda está em branco.
                with st.expander(titulo):
                    col_a, col_b = st.columns(2)
                    with col_a:
                        if opcoes_conta:
                            opcoes_com_vazio = [""] + opcoes_conta
                            indice_padrao = _indice_da_conta(opcoes_com_vazio, linha["Conta débito"])
                            selecao_debito = st.selectbox(
                                "Conta débito", options=opcoes_com_vazio, index=indice_padrao, key=f"debito_{indice}"
                            )
                            conta_debito = _codigo_da_opcao(selecao_debito)
                        else:
                            conta_debito = st.text_input(
                                "Conta débito", value=str(linha["Conta débito"]).strip(), key=f"debito_{indice}"
                            )
                    with col_b:
                        if opcoes_conta:
                            opcoes_com_vazio = [""] + opcoes_conta
                            indice_padrao = _indice_da_conta(opcoes_com_vazio, linha["Conta crédito"])
                            selecao_credito = st.selectbox(
                                "Conta crédito", options=opcoes_com_vazio, index=indice_padrao, key=f"credito_{indice}"
                            )
                            conta_credito = _codigo_da_opcao(selecao_credito)
                        else:
                            conta_credito = st.text_input(
                                "Conta crédito", value=str(linha["Conta crédito"]).strip(), key=f"credito_{indice}"
                            )

                    historico_original = str(linha["Histórico bancário (original)"])
                    numero_historico = st.text_input(
                        "Número histórico", value=str(linha["Número histórico"]).strip(), key=f"num_hist_{indice}"
                    )
                    variavel_padrao = str(linha["Variável"]).strip() or classifier.aplicar_mascara_variavel(
                        classifier.MASCARA_VARIAVEL_PADRAO, historico_original
                    )
                    variavel = st.text_input("Variável", value=variavel_padrao, key=f"variavel_{indice}")

                    salvar_regra = st.checkbox(
                        "Salvar esta associação como regra permanente no banco de dados para os próximos extratos",
                        key=f"salvar_regra_{indice}",
                        disabled=not cnpj_empresa,
                        help=None if cnpj_empresa else "Selecione uma Empresa na barra lateral para poder salvar regras.",
                    )
                    padrao_texto = st.text_input(
                        "Padrão de texto da regra (editável — evite números que mudam a cada lançamento)",
                        value=classifier.sugerir_padrao_regra(historico_original),
                        key=f"padrao_{indice}",
                        disabled=not salvar_regra,
                        help="Termo já sugerido sem datas/números voláteis. Também é usado para propagar esta "
                        "classificação a outras linhas semelhantes deste extrato, mesmo sem salvar como regra.",
                    )

                    col_aplicar, col_ignorar = st.columns(2)
                    with col_aplicar:
                        if st.button("Aplicar", key=f"aplicar_{indice}", type="primary"):
                            if not conta_debito.strip() or not conta_credito.strip():
                                st.warning("Informe a conta débito e a conta crédito antes de aplicar.")
                            else:
                                orquestrador.aplicar_revisao_manual(
                                    df_classificado, indice, conta_debito.strip(), conta_credito.strip(),
                                    numero_historico.strip(), variavel.strip(),
                                )
                                propagados = orquestrador.propagar_classificacao(
                                    df_classificado, indice, conta_banco.strip(), padrao_texto.strip()
                                )
                                if propagados:
                                    st.success(
                                        f"Aplicado aqui e propagado automaticamente para mais "
                                        f"{len(propagados)} lançamento(s) semelhante(s) neste extrato."
                                    )
                                if salvar_regra:
                                    conta_contrapartida = (
                                        conta_credito.strip()
                                        if linha["Tipo lançamento"] == extrator.TIPO_ENTRADA
                                        else conta_debito.strip()
                                    )
                                    try:
                                        with database.RegrasDB() as regras_db:
                                            orquestrador.salvar_regra_da_revisao(
                                                regras_db,
                                                padrao_texto=padrao_texto.strip(),
                                                conta_banco=conta_banco.strip(),
                                                conta_contrapartida=conta_contrapartida,
                                                numero_historico=numero_historico.strip(),
                                                formato_variavel=classifier.MASCARA_VARIAVEL_PADRAO,
                                                cnpj_empresa=cnpj_empresa.strip() or None,
                                            )
                                        st.success(
                                            "Regra salva para este CNPJ/Banco — extratos futuros com esse padrão serão "
                                            "classificados automaticamente, com a Variável ajustada à descrição de cada um."
                                        )
                                        _limpar_caches_bd()
                                    except database.RegraInvalidaError as exc:
                                        st.error(str(exc))
                                st.rerun()
                    with col_ignorar:
                        if st.button("Ignorar esta linha (não é um lançamento)", key=f"ignorar_{indice}"):
                            orquestrador.ignorar_lancamento(df_classificado, indice)
                            st.rerun()
        else:
            st.success("Todos os lançamentos foram classificados. Pronto para exportar.")

        inconsistencias_plano = st.session_state.get("inconsistencias_plano", pd.DataFrame())
        if inconsistencias_plano is not None and not inconsistencias_plano.empty:
            with st.expander(f"⚠️ {len(inconsistencias_plano)} conta(s) atribuída(s) fora do Plano de Contas"):
                st.dataframe(inconsistencias_plano, use_container_width=True)

        st.header("Exportação")

        if n_pendentes:
            st.info("Resolva os lançamentos pendentes de revisão acima antes de exportar.")
        else:
            if n_ignorados:
                st.caption(f"{n_ignorados} linha(s) marcada(s) como 'Ignorar' não entrarão no arquivo exportado.")
            df_para_exportar = df_classificado[~mascara_ignorados]

            layout_exportacao = None
            try:
                layout_exportacao = _cache_obter_layout_por_nome(nome_layout_selecionado)
            except Exception as exc:
                st.error(f"Não foi possível carregar o layout de exportação: {exc}")

            if layout_exportacao is not None:
                st.caption(
                    f"Layout selecionado: **{layout_exportacao.nome_layout}** — formato "
                    f"**{layout_exportacao.formato_arquivo.upper()}**, {len(layout_exportacao.mapeamento_colunas)} coluna(s) mapeada(s). "
                    "Troque o layout na barra lateral ou edite-o na aba 'Gerenciador de Layouts'."
                )
                try:
                    dados = exporter.gerar_bytes(df_para_exportar, layout_exportacao)
                    extensao = layout_exportacao.formato_arquivo
                    mime = (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if extensao == "xlsx"
                        else "text/csv"
                    )
                    st.download_button(
                        f"⬇️ Baixar arquivo ({layout_exportacao.nome_layout})",
                        data=dados,
                        file_name=f"extrato_exportado.{extensao}",
                        mime=mime,
                        type="primary",
                    )
                except exporter.ExportadorError as exc:
                    st.error(f"Não foi possível gerar o arquivo de exportação: {exc}")
    else:
        st.info("Envie um extrato bancário e informe a conta do Banco na barra lateral para começar.")

with tab_regras:
    _renderizar_gerenciador_regras(cnpj_empresa)

with tab_layouts:
    _renderizar_gerenciador_layouts()
