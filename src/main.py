"""
main.py — orquestrador do Importador Contábil.

Une as três etapas (extração -> classificação -> exportação) numa única
API, reutilizável tanto pela linha de comando (bloco __main__ abaixo) quanto
pela interface Streamlit (app.py) — nenhuma lógica de pipeline deve viver
duplicada em app.py; ela sempre chama as funções deste módulo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Union

import pandas as pd

import classifier
import database
import exporter
import parser as extrator


class PipelineError(Exception):
    """Erro genérico do pipeline do Importador Contábil."""


def processar_pipeline(
    caminho_extrato: Union[str, Path],
    conta_banco: str,
    caminho_razao_anterior: Optional[Union[str, Path]] = None,
    caminho_plano_contas: Optional[Union[str, Path]] = None,
    cnpj_empresa: Optional[str] = None,
    limiar_confianca: float = 80.0,
) -> dict:
    """
    Executa o pipeline completo (parser.py -> classifier.py) sobre um único
    extrato. Devolve um dicionário com:

      'classificado'          DataFrame completo: 13 colunas do Calima +
                               'Tipo lançamento', 'Histórico bancário
                               (original)' e colunas de diagnóstico
                               ('Status classificação', 'Confiança (%)',
                               'Origem da classificação').
      'pendentes'              apenas as linhas com Status == REVISAR.
      'inconsistencias_plano'  contas atribuídas fora do Plano de Contas
                               (vazio se caminho_plano_contas não informado).
      'plano_de_contas'        instância classifier.PlanoDeContas (ou None).
      'regras_db'              instância database.RegrasDB já aberta (Postgres,
                               tenant resolvido de st.secrets/variável de
                               ambiente — ver database.py) — o chamador é
                               responsável por fechá-la (regras_db.fechar())
                               quando não precisar mais cadastrar regras
                               vindas da revisão manual.
    """
    if not conta_banco or not str(conta_banco).strip():
        raise PipelineError("conta_banco é obrigatório: informe o código contábil da conta bancária do extrato.")

    mapa_historico = classifier.analisar_razao_anterior(caminho_razao_anterior) if caminho_razao_anterior else None
    plano_de_contas = classifier.ler_plano_de_contas(caminho_plano_contas) if caminho_plano_contas else None

    regras_db = database.RegrasDB()
    motor = classifier.ClassificadorLancamentos(
        regras_db=regras_db,
        conta_banco=str(conta_banco).strip(),
        mapa_historico=mapa_historico,
        plano_de_contas=plano_de_contas,
        cnpj_empresa=cnpj_empresa,
        limiar_confianca=limiar_confianca,
    )

    df_classificado = motor.classificar_extrato(caminho_extrato)
    df_pendentes = classifier.separar_pendencias(df_classificado)

    df_inconsistencias = pd.DataFrame(columns=["linha", "coluna", "conta_informada"])
    if plano_de_contas is not None:
        df_inconsistencias = plano_de_contas.validar_contas(df_classificado)

    return {
        "classificado": df_classificado,
        "pendentes": df_pendentes,
        "inconsistencias_plano": df_inconsistencias,
        "plano_de_contas": plano_de_contas,
        "regras_db": regras_db,
    }


def aplicar_revisao_manual(
    df_classificado: pd.DataFrame,
    indice,
    conta_debito: str,
    conta_credito: str,
    numero_historico: str = "",
    variavel: str = "",
) -> pd.DataFrame:
    """
    Aplica, sobre `df_classificado`, a decisão manual do usuário para o
    lançamento em `indice`: atualiza 'Conta débito'/'Conta crédito' (e
    opcionalmente 'Número histórico'/'Variável') e marca o status como
    'OK_MANUAL'. Muta `df_classificado` in place e também o retorna.
    """
    df_classificado.loc[indice, "Conta débito"] = conta_debito
    df_classificado.loc[indice, "Conta crédito"] = conta_credito
    if numero_historico:
        df_classificado.loc[indice, "Número histórico"] = numero_historico
    if variavel:
        df_classificado.loc[indice, "Variável"] = variavel
    df_classificado.loc[indice, "Status classificação"] = classifier.STATUS_MANUAL
    df_classificado.loc[indice, "Confiança (%)"] = 100.0
    df_classificado.loc[indice, "Origem da classificação"] = "Classificação manual (revisão do usuário)"
    return df_classificado


def propagar_classificacao(
    df_classificado: pd.DataFrame,
    indice_origem,
    conta_banco: str,
    padrao_texto: str = "",
) -> list:
    """
    Depois de aplicar a linha `indice_origem` (já atualizada em
    df_classificado, tipicamente logo após aplicar_revisao_manual), propaga
    a mesma classificação para as demais linhas AINDA PENDENTES
    (classifier.STATUS_REVISAR) do extrato que tenham a mesma descrição
    bancária ou que contenham `padrao_texto` — reduz o trabalho manual em
    extratos com vários meses ou lançamentos recorrentes. Linhas já
    resolvidas por regra, Razão anterior, revisão manual anterior ou
    marcadas como ignoradas não são tocadas.

    A conta de contrapartida é identificada a partir da linha de origem e
    reaplicada no lado certo (débito/crédito) conforme o tipo de cada linha
    de destino; a Variável é recalculada pela máscara padrão sobre a
    descrição de cada uma (não copiada literalmente da linha de origem).

    Devolve a lista de índices efetivamente atualizados.
    """
    linha_origem = df_classificado.loc[indice_origem]
    contrapartida = classifier.extrair_contrapartida(
        str(linha_origem["Conta débito"]).strip(),
        str(linha_origem["Conta crédito"]).strip(),
        conta_banco,
    )
    if contrapartida is None:
        return []  # não foi possível identificar o lado do banco — não há o que propagar com segurança

    numero_historico = str(linha_origem["Número histórico"]).strip()
    historico_origem_norm = classifier.normalizar_texto(str(linha_origem["Histórico bancário (original)"]))
    padrao_norm = classifier.normalizar_texto(padrao_texto) if padrao_texto and padrao_texto.strip() else ""

    atualizados = []
    for indice in df_classificado.index:
        if indice == indice_origem:
            continue

        linha = df_classificado.loc[indice]
        if linha["Status classificação"] != classifier.STATUS_REVISAR:
            continue

        historico_linha = str(linha["Histórico bancário (original)"])
        historico_norm = classifier.normalizar_texto(historico_linha)
        bate_descricao = bool(historico_norm) and historico_norm == historico_origem_norm
        bate_padrao = bool(padrao_norm) and padrao_norm in historico_norm
        if not (bate_descricao or bate_padrao):
            continue

        if linha["Tipo lançamento"] == extrator.TIPO_ENTRADA:
            conta_debito, conta_credito = conta_banco, contrapartida
        else:
            conta_debito, conta_credito = contrapartida, conta_banco

        df_classificado.loc[indice, "Conta débito"] = conta_debito
        df_classificado.loc[indice, "Conta crédito"] = conta_credito
        if numero_historico:
            df_classificado.loc[indice, "Número histórico"] = numero_historico
        df_classificado.loc[indice, "Variável"] = classifier.aplicar_mascara_variavel(
            classifier.MASCARA_VARIAVEL_PADRAO, historico_linha
        )
        df_classificado.loc[indice, "Status classificação"] = classifier.STATUS_PROPAGADO
        df_classificado.loc[indice, "Confiança (%)"] = 100.0
        df_classificado.loc[indice, "Origem da classificação"] = f"Propagado automaticamente da linha {indice_origem}."
        atualizados.append(indice)

    return atualizados


def ignorar_lancamento(df_classificado: pd.DataFrame, indice) -> pd.DataFrame:
    """
    Marca o lançamento em `indice` como classifier.STATUS_IGNORADO: usado
    quando a linha não é um lançamento de verdade (ex.: um resumo do extrato
    que passou do filtro automático de parser.py). A linha some da fila de
    pendências e é excluída da exportação final, sem exigir conta nenhuma.
    """
    df_classificado.loc[indice, "Conta débito"] = ""
    df_classificado.loc[indice, "Conta crédito"] = ""
    df_classificado.loc[indice, "Status classificação"] = classifier.STATUS_IGNORADO
    df_classificado.loc[indice, "Confiança (%)"] = 0.0
    df_classificado.loc[indice, "Origem da classificação"] = "Ignorado manualmente na revisão (não é um lançamento)."
    return df_classificado


def salvar_regra_da_revisao(
    regras_db,  # instância de database.RegrasDB
    padrao_texto: str,
    conta_banco: str,
    conta_contrapartida: str,
    numero_historico: str = "",
    formato_variavel: str = "",
    cnpj_empresa: Optional[str] = None,
) -> int:
    """
    Cadastra a decisão manual da revisão como regra permanente (Nível 1)
    para os próximos extratos, restrita a este CNPJ e a esta conta de Banco
    (o próprio `conta_banco` serve como identificador do contexto banco).
    """
    return regras_db.adicionar_regra_contrapartida(
        padrao_texto=padrao_texto,
        conta_contrapartida=conta_contrapartida,
        conta_banco=conta_banco,
        numero_historico=numero_historico,
        formato_variavel=formato_variavel,
        cnpj_empresa=cnpj_empresa,
        codigo_banco=conta_banco,
    )


# --------------------------------------------------------------------------- #
# Linha de comando
# --------------------------------------------------------------------------- #

def _construir_cli() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Importador Contábil — extrai, classifica e exporta extratos bancários para o layout Calima."
    )
    cli.add_argument("extrato", help="Caminho do extrato bancário (PDF, XLSX ou XLS).")
    cli.add_argument("conta_banco", help="Código contábil da conta bancária do extrato.")
    cli.add_argument("--razao-anterior", default=None, help="Razão contábil anterior (Excel ou PDF).")
    cli.add_argument("--plano-contas", default=None, help="Plano de Contas (Excel ou CSV).")
    cli.add_argument("--cnpj", default=None, help="CNPJ da empresa, para priorizar regras específicas.")
    cli.add_argument("--limiar-confianca", type=float, default=80.0, help="Confiança mínima (0-100) para aceitar por similaridade (padrão: %(default)s).")
    cli.add_argument("-o", "--saida", default="extrato_classificado.xlsx", help="Arquivo de saída.")
    cli.add_argument(
        "--layout", default=database.NOME_LAYOUT_PADRAO,
        help=f"Nome do layout de exportação cadastrado no Postgres (padrão: '{database.NOME_LAYOUT_PADRAO}'). "
        "Requer DATABASE_URL/TENANT_ID em .streamlit/secrets.toml ou variável de ambiente.",
    )
    cli.add_argument(
        "--exportar-mesmo-com-pendencias",
        action="store_true",
        help="Exporta mesmo havendo lançamentos marcados como REVISAR (não recomendado; use o app.py para resolvê-los antes).",
    )
    return cli


def main(argv: Optional[list] = None) -> int:
    args = _construir_cli().parse_args(argv)

    try:
        resultado = processar_pipeline(
            caminho_extrato=args.extrato,
            conta_banco=args.conta_banco,
            caminho_razao_anterior=args.razao_anterior,
            caminho_plano_contas=args.plano_contas,
            cnpj_empresa=args.cnpj,
            limiar_confianca=args.limiar_confianca,
        )
    except (
        extrator.ExtratoParserError, classifier.ClassificadorError,
        database.ConfiguracaoAusenteError, PipelineError, FileNotFoundError,
    ) as exc:
        print(f"Erro ao processar o extrato: {exc}")
        return 1

    resultado["regras_db"].fechar()
    df_classificado = resultado["classificado"]
    df_pendentes = resultado["pendentes"]

    print(f"{len(df_classificado)} lançamento(s) processado(s); {len(df_pendentes)} pendente(s) de revisão.")
    if not resultado["inconsistencias_plano"].empty:
        print(f"Atenção: {len(resultado['inconsistencias_plano'])} conta(s) atribuída(s) não constam do Plano de Contas.")

    if not df_pendentes.empty and not args.exportar_mesmo_com_pendencias:
        print(
            f"Exportação interrompida: há {len(df_pendentes)} lançamento(s) pendente(s) de revisão. "
            "Resolva-os (ex.: via 'streamlit run app.py') ou rode novamente com --exportar-mesmo-com-pendencias."
        )
        return 1

    with database.BancoDados() as db:
        layout = db.obter_layout_por_nome(args.layout)
    if layout is None:
        print(f"Layout '{args.layout}' não encontrado.")
        return 1

    try:
        exporter.exportar(df_classificado, layout, args.saida, validar=not args.exportar_mesmo_com_pendencias)
    except exporter.ExportadorError as exc:
        print(f"Erro ao exportar: {exc}")
        return 1

    print(f"Arquivo final salvo em '{args.saida}' usando o layout '{layout.nome_layout}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
