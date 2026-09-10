"""
Módulo de classificação inteligente de lançamentos bancários.

Recebe os lançamentos já extraídos/padronizados por parser.py (ver
parser.extrair_lancamentos / parser.processar_lancamentos) e preenche
'Conta débito', 'Conta crédito', 'Número histórico' e 'Variável' usando uma
cascata de prioridades:

  Nível 1 - regra fixa cadastrada pelo usuário (tabela regras_de_para, Postgres — ver database.RegrasDB);
  Nível 2 - similaridade de texto com o Razão contábil anterior;
  Nível 3 - pendente ("REVISAR"), para conferência manual.

Em qualquer um dos níveis, a conta de contrapartida encontrada é sempre
recolocada no lado correto conforme a regra contábil bancária:
  Entrada de dinheiro: Débito = conta Banco, Crédito = contrapartida.
  Saída de dinheiro:   Débito = contrapartida, Crédito = conta Banco.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd

import parser as extrator  # módulo da etapa 1: extração/padronização (parser.py)

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover
    try:
        from fuzzywuzzy import fuzz as _fuzz
    except ImportError:
        _fuzz = None


# --------------------------------------------------------------------------- #
# Exceções
# --------------------------------------------------------------------------- #

class ClassificadorError(Exception):
    """Erro genérico do módulo de classificação."""


class PlanoDeContasInvalidoError(ClassificadorError):
    """Plano de Contas sem as colunas mínimas exigidas."""


class RazaoAnteriorInvalidoError(ClassificadorError):
    """Razão contábil anterior sem as colunas mínimas exigidas."""


# --------------------------------------------------------------------------- #
# Status de classificação
# --------------------------------------------------------------------------- #

STATUS_REGRA = "OK_REGRA"
STATUS_RAZAO_ANTERIOR = "OK_RAZAO_ANTERIOR"
STATUS_REVISAR = "REVISAR"
STATUS_IGNORADO = "IGNORADO"
STATUS_MANUAL = "OK_MANUAL"
STATUS_PROPAGADO = "OK_PROPAGADO"


# --------------------------------------------------------------------------- #
# Utilidades de texto (normalização / similaridade)
# --------------------------------------------------------------------------- #

def _normalizar_texto(texto) -> str:
    if texto is None:
        return ""
    texto = str(texto).strip().lower()
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto)


# alias público — usado fora deste módulo (ex.: main.py) para comparar
# descrições de lançamentos na propagação automática entre linhas do extrato.
normalizar_texto = _normalizar_texto


def _pontuacao_similaridade(a: str, b: str) -> float:
    """Score de 0 a 100 de similaridade entre dois textos."""
    a, b = _normalizar_texto(a), _normalizar_texto(b)
    if not a or not b:
        return 0.0
    if _fuzz is not None:
        return float(_fuzz.token_sort_ratio(a, b))
    return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


def _localizar_coluna(colunas, chave: str, sinonimos: dict) -> Optional[str]:
    candidatos = sinonimos.get(chave, [chave])
    normalizadas = {col: _normalizar_texto(col) for col in colunas}

    for candidato in candidatos:
        candidato_norm = _normalizar_texto(candidato)
        for col_original, col_norm in normalizadas.items():
            if col_norm == candidato_norm:
                return col_original

    for candidato in candidatos:
        candidato_norm = _normalizar_texto(candidato)
        for col_original, col_norm in normalizadas.items():
            if candidato_norm and candidato_norm in col_norm:
                return col_original
    return None


def _texto_vazio(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() in ("nan", "none", "nat") else texto


# --------------------------------------------------------------------------- #
# Máscaras de texto para regras de classificação
#
# A persistência das regras (tabela regras_de_para) mora em database.py
# (Postgres/Supabase, multi-tenant) — aqui ficam só as funções puras de
# texto usadas tanto para sugerir/aplicar regras quanto pelo motor de
# classificação abaixo.
# --------------------------------------------------------------------------- #

MASCARA_VARIAVEL_PADRAO = "Valor referente {descricao}"

_PADRAO_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_PADRAO_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_PADRAO_DATA_EMBUTIDA = re.compile(r"\b\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\b")
_PADRAO_NUMERO_LONGO = re.compile(r"\b\d{4,}\b")


def sugerir_padrao_regra(historico: str) -> str:
    """
    Sugere um termo fixo/estável a partir do histórico bruto de um extrato,
    para servir de padrao_texto de uma regra — remove CPF/CNPJ, datas e
    sequências numéricas longas (IDs de transação, números de documento) que
    mudam a cada lançamento e impediriam a regra de bater em transações
    futuras do mesmo tipo. Se a limpeza esvaziar o texto (ex.: histórico era
    só um CNPJ), devolve o histórico original — uma regra específica demais
    é sempre mais segura que uma vazia, que bateria em qualquer lançamento.
    """
    texto = historico or ""
    texto = _PADRAO_CNPJ.sub("", texto)
    texto = _PADRAO_CPF.sub("", texto)
    texto = _PADRAO_DATA_EMBUTIDA.sub("", texto)
    texto = _PADRAO_NUMERO_LONGO.sub("", texto)
    texto = re.sub(r"\s+", " ", texto).strip(" -/.")
    return texto if len(texto) >= 3 else (historico or "").strip()


def aplicar_mascara_variavel(formato_variavel: str, descricao_extrato: str) -> str:
    """
    Aplica a máscara do campo 'Variável' de uma regra sobre a descrição do
    extrato do lançamento sendo classificado agora. `formato_variavel` pode
    conter o marcador '{descricao}'; se vazio, usa MASCARA_VARIAVEL_PADRAO.
    Uma máscara sem o marcador (texto fixo) ou malformada é devolvida como
    está.
    """
    mascara = formato_variavel.strip() if formato_variavel and formato_variavel.strip() else MASCARA_VARIAVEL_PADRAO
    try:
        return mascara.format(descricao=descricao_extrato)
    except (KeyError, IndexError):
        return mascara


# --------------------------------------------------------------------------- #
# Plano de Contas
# --------------------------------------------------------------------------- #

_SINONIMOS_PLANO_CONTAS = {
    "codigo_reduzido": ["codigo reduzido", "cod reduzido", "cod. reduzido", "reduzido", "codigo red"],
    "codigo_estruturado": ["codigo estruturado", "cod estruturado", "conta contabil", "codigo conta", "conta", "codigo"],
    "descricao": ["descricao", "descricao da conta", "nome da conta", "descricao conta"],
    "tipo_conta": ["tipo", "tipo de conta", "natureza da conta", "classificacao", "nivel", "analitica sintetica"],
}


@dataclass
class PlanoDeContas:
    """Plano de Contas da empresa, usado para validar as contas atribuídas pelo classificador."""

    df: pd.DataFrame
    col_codigo_reduzido: Optional[str]
    col_codigo_estruturado: Optional[str]
    col_descricao: Optional[str]
    col_tipo_conta: Optional[str] = None
    _codigos_sinteticos: frozenset = field(default_factory=frozenset, repr=False, compare=False)

    def eh_analitica(self, codigo: str) -> bool:
        """
        Indica se `codigo` é uma conta analítica (recebe lançamento
        diretamente) e não uma conta sintética (grupo/totalizador, que só
        soma as contas abaixo dela). Usa a coluna de tipo/natureza quando o
        Plano de Contas a informa explicitamente; senão, infere pela
        hierarquia do código estruturado — uma conta é sintética se algum
        outro código do plano começa com ela seguido de ponto (tem "filhos").
        """
        codigo = str(codigo).strip()
        if not codigo:
            return False

        if self.col_tipo_conta:
            col_codigo = self.col_codigo_estruturado or self.col_codigo_reduzido
            correspondentes = self.df[self.df[col_codigo].astype(str).str.strip() == codigo]
            if not correspondentes.empty:
                tipo_texto = _normalizar_texto(correspondentes.iloc[0][self.col_tipo_conta])
                if "analit" in tipo_texto:
                    return True
                if "sintet" in tipo_texto or "grupo" in tipo_texto or "total" in tipo_texto:
                    return False

        return codigo not in self._codigos_sinteticos

    def existe_conta(self, codigo: str) -> bool:
        codigo = str(codigo).strip()
        if not codigo:
            return False
        for col in (self.col_codigo_reduzido, self.col_codigo_estruturado):
            if col and (self.df[col].astype(str).str.strip() == codigo).any():
                return True
        return False

    def validar_contas(
        self, df_lancamentos: pd.DataFrame, colunas: tuple = ("Conta débito", "Conta crédito")
    ) -> pd.DataFrame:
        """Retorna as ocorrências de contas atribuídas que não constam do Plano de Contas."""
        inconsistencias = []
        for indice, linha in df_lancamentos.iterrows():
            for col in colunas:
                codigo = str(linha.get(col, "") or "").strip()
                if codigo and not self.existe_conta(codigo):
                    inconsistencias.append({"linha": indice, "coluna": col, "conta_informada": codigo})
        return pd.DataFrame(inconsistencias, columns=["linha", "coluna", "conta_informada"])

    def buscar_por_descricao(self, texto: str, limiar: float = 85.0) -> Optional[str]:
        """Busca fuzzy por uma conta cuja descrição mais se aproxime de `texto`."""
        col_codigo = self.col_codigo_estruturado or self.col_codigo_reduzido
        if not self.col_descricao or not col_codigo or not texto:
            return None

        melhor_codigo, melhor_score = None, 0.0
        for _, linha in self.df.iterrows():
            score = _pontuacao_similaridade(texto, linha[self.col_descricao])
            if score > melhor_score:
                melhor_score, melhor_codigo = score, str(linha[col_codigo]).strip()
        return melhor_codigo if melhor_score >= limiar else None


def ler_plano_de_contas(origem: Union[str, Path, "IO"]) -> PlanoDeContas:
    """
    Lê o Plano de Contas (Excel ou CSV) com Código Reduzido, Código
    Estruturado e Descrição. Aceita um caminho em disco (CLI) OU um objeto
    tipo arquivo em memória com `.name` (upload do Streamlit) — nesse
    segundo caso nada é gravado em disco.
    """
    if isinstance(origem, (str, Path)):
        origem = Path(origem)
        if not origem.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: '{origem}'")
        nome = origem.name
    else:
        nome = getattr(origem, "name", "") or ""
        if hasattr(origem, "seek"):
            origem.seek(0)

    extensao = Path(nome).suffix.lower()
    if extensao == ".csv":
        df = pd.read_csv(origem, dtype=str)
    elif extensao in (".xlsx", ".xls"):
        df = pd.read_excel(origem, dtype=str)
    else:
        raise PlanoDeContasInvalidoError(
            f"Formato '{extensao}' não suportado para o Plano de Contas. Utilize .xlsx, .xls ou .csv."
        )

    df.columns = [str(c).strip() for c in df.columns]
    col_reduzido = _localizar_coluna(df.columns, "codigo_reduzido", _SINONIMOS_PLANO_CONTAS)
    col_estruturado = _localizar_coluna(df.columns, "codigo_estruturado", _SINONIMOS_PLANO_CONTAS)
    col_descricao = _localizar_coluna(df.columns, "descricao", _SINONIMOS_PLANO_CONTAS)
    col_tipo_conta = _localizar_coluna(df.columns, "tipo_conta", _SINONIMOS_PLANO_CONTAS)

    if not col_reduzido and not col_estruturado:
        raise PlanoDeContasInvalidoError(
            "Não foi possível localizar a coluna de código da conta (reduzido ou estruturado) "
            f"no Plano de Contas. Colunas disponíveis: {list(df.columns)}"
        )

    codigos_sinteticos: set = set()
    if col_estruturado:
        codigos = set(df[col_estruturado].astype(str).str.strip())
        for codigo in codigos:
            if any(outro != codigo and outro.startswith(codigo + ".") for outro in codigos):
                codigos_sinteticos.add(codigo)

    return PlanoDeContas(
        df=df,
        col_codigo_reduzido=col_reduzido,
        col_codigo_estruturado=col_estruturado,
        col_descricao=col_descricao,
        col_tipo_conta=col_tipo_conta,
        _codigos_sinteticos=frozenset(codigos_sinteticos),
    )


# --------------------------------------------------------------------------- #
# Razão contábil anterior
# --------------------------------------------------------------------------- #

_SINONIMOS_RAZAO = {
    "historico": ["historico", "descricao", "complemento", "historico completo", "descricao lancamento"],
    "conta_debito": ["conta debito", "debito", "cta debito", "conta d"],
    "conta_credito": ["conta credito", "credito", "cta credito", "conta c"],
    "numero_historico": ["numero historico", "cod historico", "codigo historico", "historico padrao", "n historico"],
    "variavel": ["variavel", "var"],
}


def analisar_razao_anterior(caminho: Union[str, Path]) -> List[Dict[str, str]]:
    """
    Lê o último Razão contábil exportado (Excel ou PDF) e monta um mapa
    histórico texto -> classificação (conta_debito, conta_credito,
    numero_historico, variavel), usado no Nível 2 (inferência por
    similaridade) do motor de classificação.
    """
    df_bruto = extrator.ler_extrato(caminho)
    df_bruto.columns = [str(c).strip() for c in df_bruto.columns]

    col_historico = _localizar_coluna(df_bruto.columns, "historico", _SINONIMOS_RAZAO)
    col_debito = _localizar_coluna(df_bruto.columns, "conta_debito", _SINONIMOS_RAZAO)
    col_credito = _localizar_coluna(df_bruto.columns, "conta_credito", _SINONIMOS_RAZAO)
    col_num_historico = _localizar_coluna(df_bruto.columns, "numero_historico", _SINONIMOS_RAZAO)
    col_variavel = _localizar_coluna(df_bruto.columns, "variavel", _SINONIMOS_RAZAO)

    if not col_historico or not (col_debito or col_credito):
        raise RazaoAnteriorInvalidoError(
            "Não foi possível localizar as colunas de histórico/contas no Razão anterior. "
            f"Colunas disponíveis: {list(df_bruto.columns)}"
        )

    mapa: List[Dict[str, str]] = []
    for _, linha in df_bruto.iterrows():
        texto = _texto_vazio(linha[col_historico])
        if not texto:
            continue
        mapa.append(
            {
                "texto": texto,
                "conta_debito": _texto_vazio(linha[col_debito]) if col_debito else "",
                "conta_credito": _texto_vazio(linha[col_credito]) if col_credito else "",
                "numero_historico": _texto_vazio(linha[col_num_historico]) if col_num_historico else "",
                "variavel": _texto_vazio(linha[col_variavel]) if col_variavel else "",
            }
        )

    if not mapa:
        raise RazaoAnteriorInvalidoError("Nenhum lançamento com histórico e contas válidas foi encontrado no Razão anterior.")
    return mapa


# --------------------------------------------------------------------------- #
# Motor de classificação em cascata
# --------------------------------------------------------------------------- #

@dataclass
class ResultadoClassificacao:
    conta_debito: str
    conta_credito: str
    numero_historico: str
    variavel: str
    status: str
    confianca: float = 0.0
    origem: str = ""


def extrair_contrapartida(conta_debito: str, conta_credito: str, conta_banco: str) -> Optional[str]:
    """
    Dado um par (conta_debito, conta_credito) já classificado, identifica
    qual lado corresponde à conta do Banco e retorna o código do outro lado
    (a contrapartida). Se nenhum dos dois lados bater com conta_banco,
    retorna None (situação não-bancária, ex.: transferência entre contas
    internas cadastrada manualmente) e o par original é preservado. Público:
    usado também por main.py para propagar uma classificação manual para
    outras linhas semelhantes do mesmo extrato.
    """
    banco = str(conta_banco).strip()
    debito, credito = str(conta_debito).strip(), str(conta_credito).strip()
    if not banco:
        return None
    if debito == banco:
        return credito
    if credito == banco:
        return debito
    return None


class ClassificadorLancamentos:
    """
    Motor de classificação em cascata:
      Nível 1 - regra fixa cadastrada (tabela regras_de_para, Postgres — ver database.RegrasDB);
      Nível 2 - similaridade de texto com o Razão contábil anterior;
      Nível 3 - pendente, marcado como STATUS_REVISAR para conferência manual.

    Em todos os níveis, a conta de contrapartida encontrada é recolocada no
    lado correto (débito/crédito) conforme a regra contábil bancária: uma
    entrada de dinheiro debita o Banco e credita a contrapartida; uma saída
    credita o Banco e debita a contrapartida.
    """

    def __init__(
        self,
        regras_db,  # instância de database.RegrasDB — só precisa expor .buscar_regra(historico, cnpj_empresa, codigo_banco)
        conta_banco: str,
        mapa_historico: Optional[List[Dict[str, str]]] = None,
        plano_de_contas: Optional[PlanoDeContas] = None,
        cnpj_empresa: Optional[str] = None,
        limiar_confianca: float = 80.0,
    ):
        self.regras_db = regras_db
        self.conta_banco = conta_banco
        self.mapa_historico = mapa_historico or []
        self.plano_de_contas = plano_de_contas
        self.cnpj_empresa = cnpj_empresa
        self.limiar_confianca = limiar_confianca

    # -- níveis da cascata -------------------------------------------------- #

    def _classificar_nivel1(self, historico: str) -> Optional[ResultadoClassificacao]:
        regra = self.regras_db.buscar_regra(historico, self.cnpj_empresa, self.conta_banco)
        if regra is None:
            return None
        return ResultadoClassificacao(
            conta_debito=regra["conta_debito"] or "",
            conta_credito=regra["conta_credito"] or "",
            numero_historico=regra["numero_historico"] or "",
            variavel=aplicar_mascara_variavel(regra["formato_variavel"] or "", historico),
            status=STATUS_REGRA,
            confianca=100.0,
            origem=f"regra #{regra['id']} ('{regra['padrao_texto']}')",
        )

    def _classificar_nivel2(self, historico: str) -> Optional[ResultadoClassificacao]:
        if not self.mapa_historico or not historico:
            return None

        melhor_item, melhor_score = None, 0.0
        for item in self.mapa_historico:
            score = _pontuacao_similaridade(historico, item["texto"])
            if score > melhor_score:
                melhor_score, melhor_item = score, item

        if melhor_item is None or melhor_score < self.limiar_confianca:
            return None

        return ResultadoClassificacao(
            conta_debito=melhor_item.get("conta_debito", ""),
            conta_credito=melhor_item.get("conta_credito", ""),
            numero_historico=melhor_item.get("numero_historico", ""),
            variavel=melhor_item.get("variavel", ""),
            status=STATUS_RAZAO_ANTERIOR,
            confianca=melhor_score,
            origem=f"Razão anterior: '{melhor_item['texto']}'",
        )

    def _classificar_nivel3(self, tipo: str) -> ResultadoClassificacao:
        """
        Nenhuma regra ou lançamento similar foi encontrado. Mesmo assim, o
        lado do Banco já é conhecido (self.conta_banco + tipo do lançamento)
        e é preenchido de uma vez — só a contrapartida fica de fato pendente
        de revisão manual.
        """
        conta_debito = self.conta_banco if tipo == extrator.TIPO_ENTRADA else ""
        conta_credito = self.conta_banco if tipo == extrator.TIPO_SAIDA else ""
        return ResultadoClassificacao(
            conta_debito=conta_debito,
            conta_credito=conta_credito,
            numero_historico="",
            variavel="",
            status=STATUS_REVISAR,
            confianca=0.0,
            origem="Nenhuma regra cadastrada ou lançamento similar no Razão anterior — falta a contrapartida.",
        )

    def _aplicar_regra_bancaria(self, resultado: ResultadoClassificacao, tipo: str) -> ResultadoClassificacao:
        if resultado.status == STATUS_REVISAR:
            return resultado

        contrapartida = extrair_contrapartida(resultado.conta_debito, resultado.conta_credito, self.conta_banco)
        if contrapartida is None:
            # não foi possível identificar o lado do banco no par encontrado
            # (ex.: transferência entre contas internas) — mantém como está.
            return resultado

        if tipo == extrator.TIPO_ENTRADA:
            resultado.conta_debito, resultado.conta_credito = self.conta_banco, contrapartida
        else:
            resultado.conta_debito, resultado.conta_credito = contrapartida, self.conta_banco
        return resultado

    def classificar(self, historico: str, tipo: str) -> ResultadoClassificacao:
        """
        Classifica um único lançamento. `tipo` deve ser parser.TIPO_ENTRADA
        ou parser.TIPO_SAIDA.
        """
        resultado = (
            self._classificar_nivel1(historico)
            or self._classificar_nivel2(historico)
            or self._classificar_nivel3(tipo)
        )
        return self._aplicar_regra_bancaria(resultado, tipo)

    # -- operação em lote ----------------------------------------------------#

    def classificar_dataframe(self, df_lancamentos: pd.DataFrame) -> pd.DataFrame:
        """
        Recebe o DataFrame intermediário de parser.extrair_lancamentos
        (colunas: Data, Valor, Tipo, Histórico, Documento, CPF/CNPJ) e devolve
        o DataFrame no layout do Calima (parser.COLUNAS_CALIMA) mais três
        colunas de diagnóstico ('Status classificação', 'Confiança (%)',
        'Origem da classificação'), úteis para a fila de revisão manual.
        """
        colunas_necessarias = {"Data", "Valor", "Tipo", "Histórico"}
        faltantes = colunas_necessarias - set(df_lancamentos.columns)
        if faltantes:
            raise ClassificadorError(
                f"DataFrame de lançamentos não possui as colunas esperadas: {faltantes}. "
                "Use parser.extrair_lancamentos()/parser.processar_lancamentos() para gerar a entrada correta."
            )

        linhas = []
        for _, lanc in df_lancamentos.iterrows():
            resultado = self.classificar(lanc["Histórico"], lanc["Tipo"])
            linhas.append(
                {
                    "Data": lanc["Data"],
                    "Valor": lanc["Valor"],
                    "Conta débito": resultado.conta_debito,
                    "Conta crédito": resultado.conta_credito,
                    "Número histórico": resultado.numero_historico,
                    "Variável": resultado.variavel,
                    "Centro custo débito": "",
                    "Centro custo crédito": "",
                    "Número lote": "",
                    "Código do imóvel": "",
                    "Tipo de documento": lanc.get("Documento", ""),
                    "CPF/CNPJ": lanc.get("CPF/CNPJ", ""),
                    "Motivo de modificação do patrimônio líquido": "",
                    "Tipo lançamento": lanc["Tipo"],
                    "Histórico bancário (original)": lanc["Histórico"],
                    "Status classificação": resultado.status,
                    "Confiança (%)": round(resultado.confianca, 1),
                    "Origem da classificação": resultado.origem,
                }
            )

        return pd.DataFrame(linhas)

    def classificar_extrato(self, caminho_extrato: Union[str, Path]) -> pd.DataFrame:
        """Atalho: extrai (via parser.py) e classifica um extrato bancário em uma única chamada."""
        df_lancamentos = extrator.processar_lancamentos(caminho_extrato)
        return self.classificar_dataframe(df_lancamentos)


# --------------------------------------------------------------------------- #
# Utilidades de saída
# --------------------------------------------------------------------------- #

def separar_pendencias(df_classificado: pd.DataFrame) -> pd.DataFrame:
    """Retorna apenas as linhas marcadas como STATUS_REVISAR, para conferência manual."""
    return df_classificado[df_classificado["Status classificação"] == STATUS_REVISAR].copy()


def layout_calima(df_classificado: pd.DataFrame) -> pd.DataFrame:
    """Reduz o DataFrame classificado às exatas colunas exigidas pelo Calima, descartando o diagnóstico."""
    return df_classificado[extrator.COLUNAS_CALIMA].copy()


if __name__ == "__main__":
    # Ponto de entrada de linha de comando de verdade: main.py (usa este módulo internamente
    # e já resolve DATABASE_URL/TENANT_ID). Este bloco é só um teste manual rápido.
    import sys

    import database  # import local — evita acoplar classifier.py ao Postgres fora deste teste

    if len(sys.argv) < 3:
        print("Uso: python classifier.py <extrato> <conta_banco> [razao_anterior.xlsx] [saida.xlsx]")
        sys.exit(1)

    caminho_extrato, conta_banco_cli = sys.argv[1:3]
    caminho_razao = sys.argv[3] if len(sys.argv) > 3 else None
    arquivo_saida = sys.argv[4] if len(sys.argv) > 4 else "extrato_classificado.xlsx"

    mapa_historico_cli = analisar_razao_anterior(caminho_razao) if caminho_razao else None

    with database.RegrasDB() as regras_db_cli:
        classificador = ClassificadorLancamentos(
            regras_db=regras_db_cli, conta_banco=conta_banco_cli, mapa_historico=mapa_historico_cli
        )
        resultado_df = classificador.classificar_extrato(caminho_extrato)

    resultado_df.to_excel(arquivo_saida, index=False)
    pendentes = separar_pendencias(resultado_df)
    print(
        f"{len(resultado_df)} lançamento(s) classificado(s) e salvo(s) em '{arquivo_saida}' "
        f"({len(pendentes)} pendente(s) de revisão)."
    )
