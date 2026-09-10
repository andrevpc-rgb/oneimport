"""
Módulo de extração e padronização de extratos bancários (PDF/Excel) para o
layout de importação contábil do sistema Calima.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

import pandas as pd

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None


# --------------------------------------------------------------------------- #
# Exceções
# --------------------------------------------------------------------------- #

class ExtratoParserError(Exception):
    """Erro genérico do parser de extratos."""


class FormatoNaoSuportadoError(ExtratoParserError):
    """Extensão de arquivo não suportada."""


class ExtracaoFalhouError(ExtratoParserError):
    """Não foi possível localizar/extrair uma tabela de lançamentos no arquivo."""


class ColunaNaoEncontradaError(ExtratoParserError):
    """Uma coluna obrigatória (data ou valor) não foi identificada no extrato."""


# --------------------------------------------------------------------------- #
# Layout de saída (Calima)
# --------------------------------------------------------------------------- #

COLUNAS_CALIMA = [
    "Data",
    "Valor",
    "Conta débito",
    "Conta crédito",
    "Número histórico",
    "Variável",
    "Centro custo débito",
    "Centro custo crédito",
    "Número lote",
    "Código do imóvel",
    "Tipo de documento",
    "CPF/CNPJ",
    "Motivo de modificação do patrimônio líquido",
]

# Do ponto de vista contábil da própria conta Banco (ativo):
#   dinheiro que ENTRA aumenta o ativo  -> é um DÉBITO na conta Banco
#   dinheiro que SAI    diminui o ativo -> é um CRÉDITO na conta Banco
TIPO_ENTRADA = "Débito/Entrada"
TIPO_SAIDA = "Crédito/Saída"

_SINONIMOS_COLUNAS = {
    "data": ["data", "dt", "data lancamento", "data lanc", "dt movimento", "data mov", "dt. movimento"],
    "historico": ["historico", "descricao", "lancamento", "detalhes", "complemento", "descricao lancamento"],
    "valor": ["valor", "valor r$", "valor (r$)", "vlr", "montante"],
    "credito": ["credito", "valor credito", "entrada", "entradas", "creditos"],
    "debito": ["debito", "valor debito", "saida", "saidas", "debitos"],
    "indicador": ["indicador", "d/c", "dc", "tipo", "natureza", "tipo lancamento", "tipo de lancamento"],
    "documento": ["documento", "nr documento", "num documento", "numero documento", "doc", "n documento"],
    "cpf_cnpj": ["cpf/cnpj", "cpf", "cnpj", "cpf cnpj"],
}

_PALAVRAS_SAIDA = {"debito", "d", "saida", "pagamento", "envio", "retirada"}
_PALAVRAS_ENTRADA = {"credito", "c", "entrada", "recebimento", "deposito"}


# --------------------------------------------------------------------------- #
# Utilidades de normalização de texto/colunas
# --------------------------------------------------------------------------- #

def _normalizar_texto(texto) -> str:
    """minúsculas, sem acento, sem espaços duplicados — usado para comparar nomes."""
    if texto is None:
        return ""
    texto = str(texto).strip().lower()
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto)


def _localizar_coluna(colunas, chave: str) -> Optional[str]:
    """Encontra, entre `colunas`, a que corresponde a um dos sinônimos de `chave`."""
    candidatos = _SINONIMOS_COLUNAS.get(chave, [chave])
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


def _tem_colunas_essenciais(colunas) -> bool:
    tem_data = _localizar_coluna(colunas, "data") is not None
    tem_valor = (
        _localizar_coluna(colunas, "valor") is not None
        or _localizar_coluna(colunas, "credito") is not None
        or _localizar_coluna(colunas, "debito") is not None
    )
    return tem_data and tem_valor


def _deduplicar_colunas(colunas) -> list:
    vistas: dict = {}
    resultado = []
    for c in colunas:
        c = c if c else "coluna"
        if c in vistas:
            vistas[c] += 1
            resultado.append(f"{c}_{vistas[c]}")
        else:
            vistas[c] = 0
            resultado.append(c)
    return resultado


def _texto_vazio(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() in ("nan", "none", "nat") else texto


# --------------------------------------------------------------------------- #
# Padronização de datas
# --------------------------------------------------------------------------- #

_FORMATOS_DATA = [
    "%d/%m/%Y", "%d/%m/%y",
    "%d-%m-%Y", "%d-%m-%y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d.%m.%Y",
]


def padronizar_data(valor) -> str:
    """Converte data (string em vários formatos, datetime ou Timestamp) para 'DD/MM/AAAA'."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        raise ValueError("Data vazia/ausente.")

    if isinstance(valor, pd.Timestamp):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, (datetime.datetime, datetime.date)):
        return valor.strftime("%d/%m/%Y")

    texto = str(valor).strip()
    if not texto:
        raise ValueError("Data vazia/ausente.")

    for fmt in _FORMATOS_DATA:
        try:
            return datetime.datetime.strptime(texto, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    try:
        data_convertida = pd.to_datetime(texto, dayfirst=True, errors="raise")
        return data_convertida.strftime("%d/%m/%Y")
    except Exception as exc:
        raise ValueError(f"Não foi possível interpretar a data: '{valor}'") from exc


# --------------------------------------------------------------------------- #
# Padronização de valores monetários
# --------------------------------------------------------------------------- #

def padronizar_valor(valor) -> float:
    """
    Converte valores monetários de extratos (strings com separadores BR/US,
    símbolos de moeda, parênteses ou sufixo D/C para negativo) para float.
    """
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        raise ValueError("Valor vazio/ausente.")

    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip()
    if not texto:
        raise ValueError("Valor vazio/ausente.")

    negativo = False

    if texto.startswith("(") and texto.endswith(")"):
        negativo = True
        texto = texto[1:-1].strip()

    match_sufixo = re.match(r"^(.*?)\s*([DC])$", texto, flags=re.IGNORECASE)
    if match_sufixo:
        texto, sufixo = match_sufixo.groups()
        texto = texto.strip()
        if sufixo.upper() == "D":
            negativo = True

    texto = re.sub(r"[R$\s]", "", texto, flags=re.IGNORECASE)

    if texto.startswith("-"):
        negativo = True
        texto = texto[1:]
    elif texto.startswith("+"):
        texto = texto[1:]

    if not texto:
        raise ValueError(f"Não foi possível interpretar o valor monetário: '{valor}'")

    ultimo_ponto = texto.rfind(".")
    ultima_virgula = texto.rfind(",")

    if ultimo_ponto != -1 and ultima_virgula != -1:
        # o separador decimal é o que aparece por último: BR (1.234,56) x US (1,234.56)
        if ultima_virgula > ultimo_ponto:
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif ultima_virgula != -1:
        texto = texto.replace(".", "").replace(",", ".")
    # se só houver ponto (ou nenhum separador), assume-se decimal já padrão

    try:
        numero = float(texto)
    except ValueError as exc:
        raise ValueError(f"Não foi possível interpretar o valor monetário: '{valor}'") from exc

    return -abs(numero) if negativo else numero


# --------------------------------------------------------------------------- #
# Identificação do tipo de lançamento
# --------------------------------------------------------------------------- #

def identificar_tipo_lancamento(valor_num: float, indicador: Optional[str] = None) -> str:
    """
    Classifica o lançamento do ponto de vista da conta Banco (ativo):
      - TIPO_ENTRADA ("Débito/Entrada"): dinheiro entrou no banco -> débito na conta Banco.
      - TIPO_SAIDA   ("Crédito/Saída"): dinheiro saiu do banco -> crédito na conta Banco.

    Prioriza um indicador textual explícito do extrato (coluna "D/C", "Tipo",
    "Natureza" etc.) e usa o sinal do valor numérico como alternativa.
    """
    if indicador:
        chave = _normalizar_texto(indicador)
        if chave in _PALAVRAS_SAIDA:
            return TIPO_SAIDA
        if chave in _PALAVRAS_ENTRADA:
            return TIPO_ENTRADA

    return TIPO_SAIDA if valor_num < 0 else TIPO_ENTRADA


# --------------------------------------------------------------------------- #
# Leitura de arquivos — Excel
# --------------------------------------------------------------------------- #

def _ler_excel(origem) -> pd.DataFrame:
    """`origem` é um Path (CLI) ou um objeto tipo arquivo em memória (upload do Streamlit) — nunca é gravado em disco."""
    nome = getattr(origem, "name", str(origem))
    try:
        df = pd.read_excel(origem)
    except Exception as exc:
        raise ExtracaoFalhouError(f"Falha ao ler o arquivo Excel '{nome}': {exc}") from exc

    if df.empty:
        raise ExtracaoFalhouError(f"O arquivo Excel '{nome}' não contém dados.")

    if _tem_colunas_essenciais(df.columns):
        return df

    # extratos exportados de bancos costumam ter linhas de cabeçalho/metadados
    # antes da tabela real de lançamentos — tenta localizar a linha correta.
    for linha_cabecalho in range(1, 11):
        try:
            if hasattr(origem, "seek"):
                origem.seek(0)  # um Path reabre do zero sozinho; um stream precisa voltar ao início
            df_tentativa = pd.read_excel(origem, header=linha_cabecalho)
        except Exception:
            continue
        if _tem_colunas_essenciais(df_tentativa.columns):
            return df_tentativa

    return df


# --------------------------------------------------------------------------- #
# Leitura de arquivos — PDF
# --------------------------------------------------------------------------- #

_PADRAO_LINHA_EXTRATO_PDF = re.compile(
    r"^(?P<data>\d{2}[/.-]\d{2}[/.-]\d{2,4})\s+"
    r"(?P<historico>.+?)\s+"
    r"(?P<valor>[-+]?\(?R?\$?\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\)?)"
    r"\s*(?P<indicador>[DC])?$"
)


def _extrair_tabelas_pdf(pdf) -> Optional[pd.DataFrame]:
    tabelas_extraidas = []
    for pagina in pdf.pages:
        for tabela in pagina.extract_tables():
            if tabela and len(tabela) > 1:
                tabelas_extraidas.append(tabela)

    if not tabelas_extraidas:
        return None

    cabecalho = _deduplicar_colunas([str(c).strip() if c else "" for c in tabelas_extraidas[0][0]])
    linhas = []
    for tabela in tabelas_extraidas:
        primeira_linha = [str(c).strip() if c else "" for c in tabela[0]]
        corpo = tabela[1:] if primeira_linha == [c.split("_")[0] for c in cabecalho] else tabela
        linhas.extend(corpo)

    df = pd.DataFrame(linhas, columns=cabecalho)
    return df.dropna(how="all")


def _extrair_texto_pdf(pdf) -> Optional[pd.DataFrame]:
    registros = []
    for pagina in pdf.pages:
        texto = pagina.extract_text() or ""
        for linha in texto.splitlines():
            m = _PADRAO_LINHA_EXTRATO_PDF.match(linha.strip())
            if m:
                registros.append(m.groupdict())

    if not registros:
        return None
    return pd.DataFrame(registros)


# --------------------------------------------------------------------------- #
# Fallback específico: extratos do Nubank (PF/PJ)
#
# Não usam tabela com grade nem repetem a data em cada lançamento: a data
# aparece uma vez por dia ("04 NOV 2025"), seguida de vários lançamentos SEM
# data própria, agrupados sob "Total de entradas"/"Total de saídas" (que são
# só subtotais do dia, não lançamentos — como "Saldo do dia"). A descrição de
# cada lançamento também pode quebrar em várias linhas antes do valor aparecer.
# --------------------------------------------------------------------------- #

_MESES_PT = {
    "JAN": "01", "FEV": "02", "MAR": "03", "ABR": "04", "MAI": "05", "JUN": "06",
    "JUL": "07", "AGO": "08", "SET": "09", "OUT": "10", "NOV": "11", "DEZ": "12",
}

_PADRAO_DATA_NUBANK = re.compile(
    r"^(\d{2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\s+(\d{4})\b", re.IGNORECASE
)
_PADRAO_VALOR_FINAL_NUBANK = re.compile(r"([+-]?\s?R?\$?\s?\d{1,3}(?:\.\d{3})*,\d{2})\s*$")


def _extrair_nubank_pdf(pdf) -> Optional[pd.DataFrame]:
    registros = []
    data_atual = None
    tipo_atual = None  # TIPO_ENTRADA ou TIPO_SAIDA — contexto da seção atual do dia
    buffer_descricao: List[str] = []

    for pagina in pdf.pages:
        texto = pagina.extract_text() or ""
        for linha_bruta in texto.splitlines():
            linha = linha_bruta.strip()
            if not linha:
                continue
            linha_lower = linha.lower()

            m_data = _PADRAO_DATA_NUBANK.match(linha)
            if m_data:
                buffer_descricao.clear()
                dia, mes_abrev, ano = m_data.groups()
                mes = _MESES_PT.get(mes_abrev.upper())
                data_atual = f"{dia}/{mes}/{ano}" if mes else None
                resto_lower = linha[m_data.end():].strip().lower()
                if "total de entradas" in resto_lower:
                    tipo_atual = TIPO_ENTRADA
                elif "total de saídas" in resto_lower or "total de saidas" in resto_lower:
                    tipo_atual = TIPO_SAIDA
                continue

            if linha_lower.startswith("saldo"):
                buffer_descricao.clear()
                continue
            if "total de entradas" in linha_lower:
                tipo_atual = TIPO_ENTRADA
                buffer_descricao.clear()
                continue
            if "total de saídas" in linha_lower or "total de saidas" in linha_lower:
                tipo_atual = TIPO_SAIDA
                buffer_descricao.clear()
                continue

            if data_atual is None or tipo_atual is None:
                continue  # ainda não entramos numa seção de movimentações reconhecida

            m_valor = _PADRAO_VALOR_FINAL_NUBANK.search(linha)
            if not m_valor:
                buffer_descricao.append(linha)
                continue

            descricao_linha = linha[: m_valor.start()].strip()
            if descricao_linha:
                buffer_descricao.append(descricao_linha)
            descricao_completa = " ".join(buffer_descricao).strip()
            buffer_descricao.clear()
            if descricao_completa:
                registros.append(
                    {
                        "data": data_atual,
                        "historico": descricao_completa,
                        "valor": m_valor.group(1).replace(" ", ""),
                        "indicador": "C" if tipo_atual == TIPO_ENTRADA else "D",
                    }
                )

    if not registros:
        return None
    return pd.DataFrame(registros)


def _ler_pdf(origem) -> pd.DataFrame:
    """`origem` é um Path (CLI) ou um objeto tipo arquivo em memória (upload do Streamlit) — nunca é gravado em disco."""
    if pdfplumber is None:
        raise ExtratoParserError(
            "A biblioteca 'pdfplumber' é necessária para ler arquivos PDF. "
            "Instale com: pip install pdfplumber"
        )

    nome = getattr(origem, "name", str(origem))
    try:
        if hasattr(origem, "seek"):
            origem.seek(0)
        with pdfplumber.open(origem) as pdf:
            df_tabelas = _extrair_tabelas_pdf(pdf)
            if df_tabelas is not None and not df_tabelas.empty:
                return df_tabelas

            df_nubank = _extrair_nubank_pdf(pdf)
            if df_nubank is not None and not df_nubank.empty:
                return df_nubank

            df_texto = _extrair_texto_pdf(pdf)
    except ExtratoParserError:
        raise
    except Exception as exc:
        raise ExtracaoFalhouError(f"Falha ao ler o arquivo PDF '{nome}': {exc}") from exc

    if df_texto is None or df_texto.empty:
        raise ExtracaoFalhouError(
            f"Nenhum lançamento pôde ser localizado no PDF '{nome}' "
            "(nem em tabelas com grade, nem no padrão do Nubank, nem no texto extraído)."
        )
    return df_texto


def ler_extrato(origem: Union[str, Path, "IO"]) -> pd.DataFrame:
    """
    Lê um extrato bancário (PDF, XLSX ou XLS) e retorna um DataFrame bruto.

    Aceita um caminho de arquivo em disco (str/Path — usado pela CLI) OU um
    objeto tipo arquivo já em memória com atributo `.name` (o retorno de
    st.file_uploader do Streamlit). No segundo caso nada é gravado em disco
    em nenhum momento — sustenta a política de retenção zero de dados
    bancários da interface: o extrato existe só em RAM, pelo tempo da sessão.
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
    if extensao in (".xlsx", ".xls"):
        return _ler_excel(origem)
    if extensao == ".pdf":
        return _ler_pdf(origem)

    raise FormatoNaoSuportadoError(f"Formato '{extensao}' não suportado. Utilize .pdf, .xlsx ou .xls.")


# --------------------------------------------------------------------------- #
# Padronização -> layout Calima
# --------------------------------------------------------------------------- #

@dataclass
class MapaContas:
    """
    Contas contábeis padrão para montar o lançamento em partida dobrada.
    Contas não fornecidas ficam em branco no resultado, para classificação
    manual/posterior por um módulo de classificação contábil.

    conta_banco:                 conta contábil da própria conta bancária do extrato.
    conta_contrapartida_entrada: conta a creditar quando há uma entrada (ex.: "Clientes a Receber").
    conta_contrapartida_saida:   conta a debitar quando há uma saída (ex.: "Despesas a Classificar").
    """
    conta_banco: str = ""
    conta_contrapartida_entrada: str = ""
    conta_contrapartida_saida: str = ""


def _linha_para_calima(data_str, valor_num, tipo, historico, documento, cpf_cnpj, mapa_contas) -> dict:
    if tipo == TIPO_ENTRADA:
        conta_debito, conta_credito = mapa_contas.conta_banco, mapa_contas.conta_contrapartida_entrada
    else:
        conta_debito, conta_credito = mapa_contas.conta_contrapartida_saida, mapa_contas.conta_banco

    return {
        "Data": data_str,
        "Valor": round(abs(valor_num), 2),
        "Conta débito": conta_debito,
        "Conta crédito": conta_credito,
        "Número histórico": historico,
        "Variável": "",
        "Centro custo débito": "",
        "Centro custo crédito": "",
        "Número lote": "",
        "Código do imóvel": "",
        "Tipo de documento": documento,
        "CPF/CNPJ": cpf_cnpj,
        "Motivo de modificação do patrimônio líquido": "",
    }


def _colunas_lancamento(df_bruto: pd.DataFrame) -> dict:
    """Localiza as colunas relevantes de um extrato bruto, validando as obrigatórias."""
    cols = {
        "data": _localizar_coluna(df_bruto.columns, "data"),
        "historico": _localizar_coluna(df_bruto.columns, "historico"),
        "valor": _localizar_coluna(df_bruto.columns, "valor"),
        "credito": _localizar_coluna(df_bruto.columns, "credito"),
        "debito": _localizar_coluna(df_bruto.columns, "debito"),
        "indicador": _localizar_coluna(df_bruto.columns, "indicador"),
        "documento": _localizar_coluna(df_bruto.columns, "documento"),
        "cpf_cnpj": _localizar_coluna(df_bruto.columns, "cpf_cnpj"),
    }

    if not cols["data"]:
        raise ColunaNaoEncontradaError(
            f"Não foi possível localizar a coluna de data. Colunas disponíveis: {list(df_bruto.columns)}"
        )
    if not cols["valor"] and not (cols["credito"] or cols["debito"]):
        raise ColunaNaoEncontradaError(
            "Não foi possível localizar a coluna de valor (nem colunas separadas de "
            f"débito/crédito). Colunas disponíveis: {list(df_bruto.columns)}"
        )
    return cols


def _eh_linha_nao_transacional(historico: str) -> bool:
    """
    Identifica linhas de resumo do extrato (saldo do dia, saldo anterior,
    saldo bloqueado etc.) que os bancos intercalam entre os lançamentos reais.
    Elas têm data e valor como qualquer lançamento, mas não são movimentações
    — importá-las geraria partidas dobradas falsas.
    """
    return _normalizar_texto(historico).startswith("saldo")


def _extrair_linha(linha, cols: dict) -> dict:
    """Padroniza uma linha bruta em {data_str, valor_num (sempre positivo), tipo, historico, documento, cpf_cnpj}."""
    historico = _texto_vazio(linha[cols["historico"]]) if cols["historico"] else ""
    if _eh_linha_nao_transacional(historico):
        raise ValueError(f"Linha ignorada (é um resumo de saldo do extrato, não um lançamento): '{historico}'")

    data_str = padronizar_data(linha[cols["data"]])
    indicador_tipo = _texto_vazio(linha[cols["indicador"]]) if cols["indicador"] else None

    if cols["valor"]:
        valor_num = padronizar_valor(linha[cols["valor"]])
        tipo = identificar_tipo_lancamento(valor_num, indicador_tipo)
        valor_num = abs(valor_num)
    else:
        bruto_credito = _texto_vazio(linha[cols["credito"]]) if cols["credito"] else ""
        bruto_debito = _texto_vazio(linha[cols["debito"]]) if cols["debito"] else ""
        credito_preenchido = bruto_credito not in ("", "0", "0,00", "0.00")
        debito_preenchido = bruto_debito not in ("", "0", "0,00", "0.00")

        if credito_preenchido:
            valor_num = abs(padronizar_valor(bruto_credito))
            tipo = TIPO_ENTRADA
        elif debito_preenchido:
            valor_num = abs(padronizar_valor(bruto_debito))
            tipo = TIPO_SAIDA
        else:
            raise ValueError("Linha sem valor de débito ou crédito preenchido.")

    return {
        "data_str": data_str,
        "valor_num": valor_num,
        "tipo": tipo,
        "historico": historico,
        "documento": _texto_vazio(linha[cols["documento"]]) if cols["documento"] else "",
        "cpf_cnpj": _texto_vazio(linha[cols["cpf_cnpj"]]) if cols["cpf_cnpj"] else "",
    }


def padronizar_extrato(
    df_bruto: pd.DataFrame,
    mapa_contas: Optional[MapaContas] = None,
    ignorar_linhas_invalidas: bool = True,
) -> pd.DataFrame:
    """
    Recebe um DataFrame bruto (lido de PDF ou Excel, em formato livre) e
    retorna um DataFrame no layout de importação do Calima, com exatamente
    as colunas exigidas (COLUNAS_CALIMA).
    """
    mapa_contas = mapa_contas or MapaContas()

    df_bruto = df_bruto.copy()
    df_bruto.columns = [str(c).strip() for c in df_bruto.columns]
    cols = _colunas_lancamento(df_bruto)

    linhas_padronizadas = []
    erros = []

    for indice, linha in df_bruto.iterrows():
        try:
            campos = _extrair_linha(linha, cols)
            linhas_padronizadas.append(
                _linha_para_calima(
                    data_str=campos["data_str"],
                    valor_num=campos["valor_num"],
                    tipo=campos["tipo"],
                    historico=campos["historico"],
                    documento=campos["documento"],
                    cpf_cnpj=campos["cpf_cnpj"],
                    mapa_contas=mapa_contas,
                )
            )
        except (ValueError, KeyError) as exc:
            erros.append((indice, str(exc)))
            if not ignorar_linhas_invalidas:
                raise ExtratoParserError(f"Erro na linha {indice}: {exc}") from exc

    if not linhas_padronizadas:
        raise ExtracaoFalhouError(
            f"Nenhuma linha válida pôde ser padronizada a partir do extrato. Erros encontrados: {erros}"
        )

    if erros:
        warnings.warn(f"{len(erros)} linha(s) do extrato foram ignoradas por erro de formatação: {erros}")

    return pd.DataFrame(linhas_padronizadas, columns=COLUNAS_CALIMA)


def extrair_lancamentos(df_bruto: pd.DataFrame, ignorar_linhas_invalidas: bool = True) -> pd.DataFrame:
    """
    Extrai os lançamentos de um extrato bruto (Excel/PDF) para uma
    representação intermediária que preserva o texto original do histórico e
    o tipo contábil (TIPO_ENTRADA/TIPO_SAIDA) de cada lançamento — é esta a
    entrada esperada pelo módulo de classificação (classifier.py), já que
    padronizar_extrato() devolve as contas em branco e descarta o tipo.

    Colunas retornadas: Data, Valor (sempre positivo), Tipo, Histórico, Documento, CPF/CNPJ.
    """
    df_bruto = df_bruto.copy()
    df_bruto.columns = [str(c).strip() for c in df_bruto.columns]
    cols = _colunas_lancamento(df_bruto)

    registros = []
    erros = []

    for indice, linha in df_bruto.iterrows():
        try:
            campos = _extrair_linha(linha, cols)
            registros.append(
                {
                    "Data": campos["data_str"],
                    "Valor": round(campos["valor_num"], 2),
                    "Tipo": campos["tipo"],
                    "Histórico": campos["historico"],
                    "Documento": campos["documento"],
                    "CPF/CNPJ": campos["cpf_cnpj"],
                }
            )
        except (ValueError, KeyError) as exc:
            erros.append((indice, str(exc)))
            if not ignorar_linhas_invalidas:
                raise ExtratoParserError(f"Erro na linha {indice}: {exc}") from exc

    if not registros:
        raise ExtracaoFalhouError(
            f"Nenhuma linha válida pôde ser extraída do extrato. Erros encontrados: {erros}"
        )
    if erros:
        warnings.warn(f"{len(erros)} linha(s) do extrato foram ignoradas por erro de formatação: {erros}")

    return pd.DataFrame(registros, columns=["Data", "Valor", "Tipo", "Histórico", "Documento", "CPF/CNPJ"])


# --------------------------------------------------------------------------- #
# Funções de alto nível
# --------------------------------------------------------------------------- #

def processar_extrato(
    caminho: Union[str, Path],
    mapa_contas: Optional[MapaContas] = None,
    ignorar_linhas_invalidas: bool = True,
) -> pd.DataFrame:
    """Lê um extrato bancário (PDF, XLSX ou XLS) e devolve o DataFrame já padronizado no layout Calima."""
    df_bruto = ler_extrato(caminho)
    return padronizar_extrato(df_bruto, mapa_contas=mapa_contas, ignorar_linhas_invalidas=ignorar_linhas_invalidas)


def processar_lancamentos(caminho: Union[str, Path], ignorar_linhas_invalidas: bool = True) -> pd.DataFrame:
    """Lê um extrato bancário e devolve os lançamentos no formato intermediário (ver extrair_lancamentos)."""
    df_bruto = ler_extrato(caminho)
    return extrair_lancamentos(df_bruto, ignorar_linhas_invalidas=ignorar_linhas_invalidas)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python parser.py <caminho_do_extrato> [caminho_saida.xlsx]")
        sys.exit(1)

    arquivo_saida = sys.argv[2] if len(sys.argv) > 2 else "extrato_padronizado.xlsx"
    resultado = processar_extrato(sys.argv[1])
    resultado.to_excel(arquivo_saida, index=False)
    print(f"Extrato padronizado com {len(resultado)} lançamento(s) salvo em '{arquivo_saida}'.")
