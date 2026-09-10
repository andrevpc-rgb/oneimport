"""
Motor de exportação dinâmico (multi-ERP).

Substitui o antigo exporter_calima.py, que só sabia gerar o layout fixo do
Calima. Agora a formatação de saída é dirigida por um database.LayoutExportacao
(nome, formato de arquivo, delimitador, formato de data e o mapeamento de
quais colunas do sistema viram quais colunas do arquivo final, em que
ordem) — o layout "Calima ERP" continua existindo como o padrão do sistema
(ver database._layout_padrao_calima), mas qualquer outro ERP pode ser
cadastrado sem alterar código.

A validação de que os lançamentos estão prontos para exportação (contas
preenchidas, data e valor válidos) é sempre feita sobre as colunas fixas do
sistema (parser.COLUNAS_CALIMA), antes de aplicar o mapeamento do layout —
é uma regra de negócio independente de qual ERP vai receber o arquivo.
"""

from __future__ import annotations

import datetime
import io
import re
from pathlib import Path
from typing import Optional, Union

import pandas as pd

import database
import parser as extrator


class ExportadorError(Exception):
    """Erro genérico do motor de exportação."""


_PADRAO_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_FORMATO_DATA_INTERNO = "%d/%m/%Y"  # produzido por parser.padronizar_data
_FORMATOS_SUPORTADOS = ("xlsx", "csv", "txt")


# --------------------------------------------------------------------------- #
# Validação (independente do layout de saída)
# --------------------------------------------------------------------------- #

def validar_lancamentos(df_classificado: pd.DataFrame) -> pd.DataFrame:
    """
    Verifica se cada linha de `df_classificado` (colunas fixas do sistema,
    ver parser.COLUNAS_CALIMA) está pronta para exportação, qualquer que
    seja o layout de saída escolhido depois. Retorna as linhas com problema
    e o(s) motivo(s); DataFrame vazio significa "tudo certo".
    """
    problemas = []
    for indice, linha in df_classificado.iterrows():
        motivos = []

        if not str(linha.get("Conta débito", "")).strip():
            motivos.append("Conta débito em branco")
        if not str(linha.get("Conta crédito", "")).strip():
            motivos.append("Conta crédito em branco")

        if not _PADRAO_DATA.match(str(linha.get("Data", "")).strip()):
            motivos.append(f"Data fora do padrão DD/MM/AAAA: '{linha.get('Data')}'")

        try:
            valor = float(linha.get("Valor"))
            if pd.isna(valor) or valor <= 0:
                motivos.append(f"Valor inválido: '{linha.get('Valor')}'")
        except (TypeError, ValueError):
            motivos.append(f"Valor inválido: '{linha.get('Valor')}'")

        if motivos:
            problemas.append({"linha": indice, "motivos": "; ".join(motivos)})

    return pd.DataFrame(problemas, columns=["linha", "motivos"])


def _levantar_se_invalido(df_classificado: pd.DataFrame) -> None:
    problemas = validar_lancamentos(df_classificado)
    if not problemas.empty:
        detalhes = "\n".join(f"  linha {p['linha']}: {p['motivos']}" for p in problemas.to_dict("records"))
        raise ExportadorError(f"{len(problemas)} lançamento(s) não estão prontos para exportação:\n{detalhes}")


# --------------------------------------------------------------------------- #
# Formatação dirigida por layout
# --------------------------------------------------------------------------- #

def _reformatar_data(valor, formato_saida: str) -> str:
    """Reconverte uma data no formato interno (DD/MM/AAAA) para `formato_saida` (padrão strftime)."""
    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "none", "nat"):
        return ""
    try:
        data = datetime.datetime.strptime(texto, _FORMATO_DATA_INTERNO)
    except ValueError:
        return texto  # não reconhecida no formato interno — devolve como veio
    try:
        return data.strftime(formato_saida)
    except (ValueError, TypeError):
        return texto


def formatar_layout(df_classificado: pd.DataFrame, layout: "database.LayoutExportacao") -> pd.DataFrame:
    """
    Monta o DataFrame de saída conforme layout.mapeamento_colunas: uma
    coluna por item, na ordem definida, com o cabeçalho 'coluna_saida' e os
    valores lidos de 'coluna_saida'/'coluna_sistema' (em branco se a coluna
    do sistema não existir em df_classificado). A coluna 'Valor' é mantida
    numérica; 'Data' é reformatada conforme layout.formato_data.
    """
    if not layout.mapeamento_colunas:
        raise ExportadorError(f"O layout '{layout.nome_layout}' não tem nenhuma coluna mapeada.")

    colunas = []
    for item in layout.mapeamento_colunas:
        nome_sistema = str(item.get("coluna_sistema", "")).strip()
        if not nome_sistema:
            continue
        nome_saida = str(item.get("coluna_saida", "")).strip()
        if not nome_saida:
            # nunca deixa o texto interno do sentinela "vazio" vazar como cabeçalho do arquivo
            nome_saida = "" if nome_sistema == database.COLUNA_VAZIA else nome_sistema

        if nome_sistema == database.COLUNA_VAZIA:
            # coluna reservada/em branco do layout (ex.: 1ª coluna do Questor) — sem
            # correspondente no sistema, cada célula sai vazia de propósito.
            serie = pd.Series([""] * len(df_classificado), index=df_classificado.index)
        elif nome_sistema in df_classificado.columns:
            serie = df_classificado[nome_sistema]
        else:
            serie = pd.Series([""] * len(df_classificado), index=df_classificado.index)

        if nome_sistema == "Valor":
            serie = pd.to_numeric(serie, errors="coerce").round(2)
        elif nome_sistema == "Data":
            serie = serie.apply(lambda v: _reformatar_data(v, layout.formato_data))
        else:
            serie = serie.fillna("").astype(str).replace({"nan": "", "None": "", "NaT": ""})

        colunas.append(serie.rename(nome_saida).reset_index(drop=True))

    if not colunas:
        raise ExportadorError(f"O layout '{layout.nome_layout}' não tem nenhuma coluna mapeada.")
    return pd.concat(colunas, axis=1)


def _validar_formato(formato: str, layout_nome: str) -> str:
    formato = (formato or "").lower().lstrip(".")
    if formato not in _FORMATOS_SUPORTADOS:
        raise ExportadorError(
            f"Layout '{layout_nome}': formato de arquivo '{formato}' não suportado. "
            f"Utilize um de: {_FORMATOS_SUPORTADOS}."
        )
    return formato


# --------------------------------------------------------------------------- #
# Geração de arquivo (disco ou bytes em memória)
# --------------------------------------------------------------------------- #

def gerar_bytes(df_classificado: pd.DataFrame, layout: "database.LayoutExportacao", validar: bool = True) -> bytes:
    """Valida (opcional) e formata `df_classificado` conforme `layout`, devolvendo os bytes prontos para download."""
    if validar:
        _levantar_se_invalido(df_classificado)

    df_saida = formatar_layout(df_classificado, layout)
    formato = _validar_formato(layout.formato_arquivo, layout.nome_layout)

    if formato == "xlsx":
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df_saida.to_excel(writer, index=False, sheet_name=(layout.nome_layout or "Exportação")[:31])
        return buffer.getvalue()

    texto = df_saida.to_csv(index=False, sep=layout.delimitador or ";", decimal=",")
    return texto.encode("utf-8-sig")


def exportar(
    df_classificado: pd.DataFrame,
    layout: "database.LayoutExportacao",
    caminho: Union[str, Path],
    validar: bool = True,
) -> pd.DataFrame:
    """Valida (opcional), formata conforme `layout` e grava o arquivo em `caminho`. Devolve o DataFrame de saída."""
    if validar:
        _levantar_se_invalido(df_classificado)

    df_saida = formatar_layout(df_classificado, layout)
    formato = _validar_formato(layout.formato_arquivo, layout.nome_layout)
    caminho = Path(caminho)

    if formato == "xlsx":
        df_saida.to_excel(caminho, index=False)
    else:
        df_saida.to_csv(caminho, index=False, sep=layout.delimitador or ";", decimal=",", encoding="utf-8-sig")

    return df_saida


if __name__ == "__main__":
    # Requer DATABASE_URL/TENANT_ID em .streamlit/secrets.toml ou variável de ambiente (ver database.py).
    import sys

    if len(sys.argv) < 3:
        print("Uso: python exporter.py <entrada.xlsx> <saida> [nome_do_layout]")
        sys.exit(1)

    caminho_saida = sys.argv[2]
    nome_layout = sys.argv[3] if len(sys.argv) > 3 else database.NOME_LAYOUT_PADRAO

    df_entrada = pd.read_excel(sys.argv[1])
    with database.BancoDados() as db:
        layout_cli = db.obter_layout_por_nome(nome_layout)
    if layout_cli is None:
        print(f"Layout '{nome_layout}' não encontrado.")
        sys.exit(1)

    exportar(df_entrada, layout_cli, caminho_saida)
    print(f"Arquivo exportado em '{caminho_saida}' usando o layout '{nome_layout}'.")
