"""
database.py — Persistência multi-tenant no Postgres (Supabase) via SQLAlchemy.

Gerencia três tabelas (ver schema.sql, na raiz do projeto): `empresas`,
`layouts_exportacao` e `regras_de_para` — nunca dados de extrato/lançamento
bancário, que existem só em `st.session_state` durante a sessão (ver
parser.py/classifier.py/app.py).

Toda consulta é isolada por tenant_id — cada escritório (tenant) só enxerga
os próprios dados. A exceção deliberada é `layouts_exportacao`: um layout
com tenant_id NULL é global do sistema (caso do "Calima ERP"), visível para
todos os tenants.

O tenant ativo vem do login (tabela `usuarios` — ver UsuariosDB e
_obter_tenant_id): o próprio `usuarios.id` é usado como tenant_id em todas
as outras tabelas. st.secrets["TENANT_ID"] / variável de ambiente TENANT_ID
continuam funcionando como atalho para uso via linha de comando (main.py),
sem precisar logar. A connection string do Postgres vem de
st.secrets["DATABASE_URL"] com fallback para a variável de ambiente
DATABASE_URL (essa sim sempre obrigatória, inclusive em produção/Render).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as secrets_modulo
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

import parser as extrator  # COLUNAS_CALIMA — base do layout padrão "Calima ERP"


class BancoDadosError(Exception):
    """Erro genérico deste módulo."""


class ConfiguracaoAusenteError(BancoDadosError):
    """DATABASE_URL não configurada, ou nenhum tenant ativo (sem login e sem TENANT_ID)."""


class EmpresaInvalidaError(BancoDadosError):
    """Empresa cadastrada com dados inválidos (ex.: CNPJ vazio ou duplicado)."""


class LayoutInvalidoError(BancoDadosError):
    """Layout de exportação cadastrado com dados inválidos."""


class RegraInvalidaError(BancoDadosError):
    """Regra de classificação cadastrada com dados inválidos."""


class UsuarioInvalidoError(BancoDadosError):
    """Cadastro ou login de escritório inválido (ex.: e-mail duplicado, credenciais incorretas)."""


NOME_LAYOUT_PADRAO = "Calima ERP"

# Opção especial de mapeamento: uma coluna do arquivo final que não vem de
# nenhuma coluna do sistema, sempre gerada em branco (ex.: a 1ª coluna do
# layout do Questor, que é reservada/vazia). Ver exporter.formatar_layout.
COLUNA_VAZIA = "— Vazio / Constante —"

# Colunas do sistema disponíveis para mapear num layout de exportação: a
# opção de coluna vazia, as 13 colunas do Calima (parser.COLUNAS_CALIMA) e
# mais duas colunas de apoio que alguns ERPs podem querer exportar também.
COLUNAS_SISTEMA_DISPONIVEIS = [COLUNA_VAZIA] + list(extrator.COLUNAS_CALIMA) + [
    "Tipo lançamento",
    "Histórico bancário (original)",
]


# --------------------------------------------------------------------------- #
# Configuração / conexão
# --------------------------------------------------------------------------- #

def _obter_segredo(chave: str) -> Optional[str]:
    """Lê `chave` de st.secrets (.streamlit/secrets.toml) se existir; senão, tenta a variável de ambiente."""
    try:
        valor = st.secrets[chave]
        if valor:
            return str(valor)
    except Exception:
        pass  # sem secrets.toml carregado, ou chave ausente — cai para variável de ambiente
    return os.environ.get(chave) or None


def _obter_database_url() -> str:
    """Resolve a connection string do Postgres (Supabase)."""
    url = _obter_segredo("DATABASE_URL")
    if not url:
        raise ConfiguracaoAusenteError(
            "DATABASE_URL não configurada. Defina em .streamlit/secrets.toml (veja "
            "src/.streamlit/secrets.toml.example) ou na variável de ambiente DATABASE_URL."
        )
    return url


def _obter_tenant_id() -> str:
    """
    Resolve o tenant_id ativo: prioriza a sessão autenticada
    (st.session_state["tenant_id"], definida no login — ver UsuariosDB e
    app.py) — é o caminho normal da interface multi-escritório. Cai para
    st.secrets["TENANT_ID"] / variável de ambiente TENANT_ID como atalho
    para uso via linha de comando (main.py), sem precisar logar.
    """
    try:
        tenant_sessao = st.session_state.get("tenant_id")
        if tenant_sessao:
            return tenant_sessao
    except Exception:
        pass  # sem contexto de sessão Streamlit ativo (ex.: rodando via CLI)

    tenant_id = _obter_segredo("TENANT_ID")
    if not tenant_id:
        raise ConfiguracaoAusenteError(
            "Nenhum tenant ativo. Faça login no OneImport, ou defina TENANT_ID em "
            ".streamlit/secrets.toml / variável de ambiente para uso via linha de comando."
        )
    return tenant_id


@lru_cache(maxsize=1)
def obter_engine() -> Engine:
    """Engine SQLAlchemy compartilhada (um pool de conexões por processo)."""
    return create_engine(_obter_database_url(), pool_pre_ping=True)


def esta_configurado() -> bool:
    """True se DATABASE_URL já foi configurada (secrets ou variável de ambiente) — pré-requisito para logar ou usar a CLI."""
    try:
        _obter_database_url()
        return True
    except ConfiguracaoAusenteError:
        return False


# --------------------------------------------------------------------------- #
# Senhas (PBKDF2-HMAC-SHA256, stdlib — sem dependência nova)
# --------------------------------------------------------------------------- #

_ITERACOES_SENHA = 260_000  # recomendação atual (OWASP) para PBKDF2-HMAC-SHA256


def _hash_senha(senha: str) -> str:
    """Gera um hash auto-contido 'pbkdf2_sha256$iteracoes$salt_hex$hash_hex' — sem precisar de coluna de salt separada."""
    salt = secrets_modulo.token_bytes(16)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, _ITERACOES_SENHA)
    return f"pbkdf2_sha256${_ITERACOES_SENHA}${salt.hex()}${hash_bytes.hex()}"


def _verificar_senha(senha: str, senha_hash_armazenado: str) -> bool:
    """Confere `senha` contra o hash salvo, em tempo constante (hmac.compare_digest) para evitar timing attack."""
    try:
        algoritmo, iteracoes_str, salt_hex, hash_hex = (senha_hash_armazenado or "").split("$")
        if algoritmo != "pbkdf2_sha256":
            return False
        hash_calculado = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(salt_hex), int(iteracoes_str))
        return hmac.compare_digest(hash_calculado.hex(), hash_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def tenant_id_ativo() -> Optional[str]:
    """Tenant ativo (sessão logada ou TENANT_ID), ou None se nenhum dos dois estiver disponível (nunca levanta exceção)."""
    try:
        return _obter_tenant_id()
    except ConfiguracaoAusenteError:
        return None


# --------------------------------------------------------------------------- #
# Usuários / escritórios (login e cadastro — cada usuário É um tenant)
# --------------------------------------------------------------------------- #

class UsuariosDB:
    """
    Cadastro e autenticação de escritórios (tabela `usuarios`). O próprio
    `id` gerado para cada usuário é usado como tenant_id em todas as
    outras tabelas — nesta primeira versão, um login corresponde a um
    escritório inteiro (sem múltiplos usuários por escritório).
    """

    def __init__(self):
        self._engine = obter_engine()

    def cadastrar(self, email: str, senha: str, nome_escritorio: str) -> Dict[str, str]:
        """Cria um novo escritório (tenant). Devolve {'id', 'email', 'nome_escritorio'}."""
        email = (email or "").strip().lower()
        nome_escritorio = (nome_escritorio or "").strip()
        if not email or "@" not in email:
            raise UsuarioInvalidoError("Informe um e-mail válido.")
        if not senha or len(senha) < 6:
            raise UsuarioInvalidoError("A senha precisa ter pelo menos 6 caracteres.")
        if not nome_escritorio:
            raise UsuarioInvalidoError("Informe o nome do escritório.")

        sql = text(
            """INSERT INTO usuarios (email, senha_hash, nome_escritorio)
               VALUES (:email, :senha_hash, :nome_escritorio)
               RETURNING id, email, nome_escritorio"""
        )
        try:
            with self._engine.begin() as conn:
                linha = conn.execute(
                    sql, {"email": email, "senha_hash": _hash_senha(senha), "nome_escritorio": nome_escritorio}
                ).mappings().first()
        except IntegrityError as exc:
            raise UsuarioInvalidoError(f"Já existe um escritório cadastrado com o e-mail '{email}'.") from exc

        return {"id": str(linha["id"]), "email": linha["email"], "nome_escritorio": linha["nome_escritorio"]}

    def autenticar(self, email: str, senha: str) -> Dict[str, str]:
        """Confere e-mail/senha. Devolve {'id', 'email', 'nome_escritorio'} ou levanta UsuarioInvalidoError."""
        email = (email or "").strip().lower()
        if not email or not senha:
            raise UsuarioInvalidoError("Informe e-mail e senha.")

        sql = text("SELECT id, email, senha_hash, nome_escritorio FROM usuarios WHERE email = :email")
        with self._engine.connect() as conn:
            linha = conn.execute(sql, {"email": email}).mappings().first()

        # mesma mensagem para e-mail inexistente ou senha errada — não revela se o e-mail existe
        if linha is None or not linha["senha_hash"] or not _verificar_senha(senha, linha["senha_hash"]):
            raise UsuarioInvalidoError("E-mail ou senha incorretos.")

        return {"id": str(linha["id"]), "email": linha["email"], "nome_escritorio": linha["nome_escritorio"]}


def _decodificar_mapeamento(valor) -> List[Dict[str, str]]:
    """mapeamento_colunas vem como JSONB — a maioria dos drivers já decodifica para list/dict sozinha."""
    if isinstance(valor, str):
        try:
            return json.loads(valor)
        except (TypeError, ValueError):
            return []
    return valor or []


# --------------------------------------------------------------------------- #
# Layouts de exportação (decodificados)
# --------------------------------------------------------------------------- #

@dataclass
class LayoutExportacao:
    """Layout de exportação já decodificado (mapeamento_colunas -> lista de dicts)."""

    id: int
    nome_layout: str
    formato_arquivo: str
    delimitador: str
    formato_data: str
    mapeamento_colunas: List[Dict[str, str]]


# --------------------------------------------------------------------------- #
# Empresas + Layouts de exportação
# --------------------------------------------------------------------------- #

class BancoDados:
    """
    Empresas e layouts de exportação no Postgres (Supabase), isolados por
    tenant_id — exceto layouts globais do sistema (tenant_id NULL, ex. o
    "Calima ERP"), visíveis para todos os tenants. Não guarda uma conexão
    persistente: cada operação usa o pool compartilhado (obter_engine()).
    O uso como context manager é opcional, mantido por compatibilidade.
    """

    _COLUNAS_LAYOUT_ATUALIZAVEIS = {"nome_layout", "formato_arquivo", "delimitador", "formato_data", "mapeamento_colunas"}
    _COLUNAS_EMPRESA_ATUALIZAVEIS = {"cnpj", "razao_social", "codigo_sistema"}

    def __init__(self, tenant_id: Optional[str] = None):
        self.tenant_id = tenant_id or _obter_tenant_id()
        self._engine = obter_engine()

    def __enter__(self) -> "BancoDados":
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def fechar(self) -> None:
        """Sem efeito — mantido por compatibilidade; o pool de conexões cuida do ciclo de vida."""

    # ---- Empresas ------------------------------------------------------- #

    def listar_empresas(self) -> pd.DataFrame:
        sql = text("SELECT * FROM empresas WHERE tenant_id = :tenant_id ORDER BY razao_social")
        return pd.read_sql_query(sql, self._engine, params={"tenant_id": self.tenant_id})

    def adicionar_empresa(self, cnpj: str, razao_social: str, codigo_sistema: str = "") -> int:
        cnpj = (cnpj or "").strip()
        razao_social = (razao_social or "").strip()
        if not cnpj:
            raise EmpresaInvalidaError("CNPJ não pode ser vazio.")
        if not razao_social:
            raise EmpresaInvalidaError("Razão social não pode ser vazia.")

        sql = text(
            """INSERT INTO empresas (tenant_id, cnpj, razao_social, codigo_sistema)
               VALUES (:tenant_id, :cnpj, :razao_social, :codigo_sistema)
               RETURNING id"""
        )
        try:
            with self._engine.begin() as conn:
                resultado = conn.execute(
                    sql,
                    {
                        "tenant_id": self.tenant_id, "cnpj": cnpj,
                        "razao_social": razao_social, "codigo_sistema": codigo_sistema or "",
                    },
                )
                return resultado.scalar_one()
        except IntegrityError as exc:
            raise EmpresaInvalidaError(f"Já existe uma empresa cadastrada com o CNPJ '{cnpj}'.") from exc

    def atualizar_empresa(self, id_empresa: int, **campos) -> None:
        campos_validos = {k: v for k, v in campos.items() if k in self._COLUNAS_EMPRESA_ATUALIZAVEIS and v is not None}
        if not campos_validos:
            return
        set_clause = ", ".join(f"{c} = :{c}" for c in campos_validos)
        sql = text(f"UPDATE empresas SET {set_clause} WHERE id = :id AND tenant_id = :tenant_id")
        try:
            with self._engine.begin() as conn:
                conn.execute(sql, {**campos_validos, "id": id_empresa, "tenant_id": self.tenant_id})
        except IntegrityError as exc:
            raise EmpresaInvalidaError(f"Não foi possível atualizar a empresa: {exc}") from exc

    def remover_empresa(self, id_empresa: int) -> None:
        sql = text("DELETE FROM empresas WHERE id = :id AND tenant_id = :tenant_id")
        with self._engine.begin() as conn:
            conn.execute(sql, {"id": id_empresa, "tenant_id": self.tenant_id})

    def buscar_empresa_por_cnpj(self, cnpj: str):
        sql = text("SELECT * FROM empresas WHERE tenant_id = :tenant_id AND cnpj = :cnpj")
        with self._engine.connect() as conn:
            return conn.execute(sql, {"tenant_id": self.tenant_id, "cnpj": cnpj}).mappings().first()

    # ---- Layouts de exportação ------------------------------------------ #

    def listar_layouts(self) -> pd.DataFrame:
        """Layouts globais do sistema (tenant_id NULL) + layouts próprios deste tenant."""
        sql = text(
            """SELECT * FROM layouts_exportacao
               WHERE tenant_id IS NULL OR tenant_id = :tenant_id
               ORDER BY nome_layout"""
        )
        df = pd.read_sql_query(sql, self._engine, params={"tenant_id": self.tenant_id})
        if "mapeamento_colunas" in df.columns:
            df["mapeamento_colunas"] = df["mapeamento_colunas"].apply(_decodificar_mapeamento)
        return df

    @staticmethod
    def _linha_para_layout(linha) -> LayoutExportacao:
        return LayoutExportacao(
            id=linha["id"],
            nome_layout=linha["nome_layout"],
            formato_arquivo=(linha["formato_arquivo"] or "xlsx").lower(),
            delimitador=linha["separador"] or ";",
            formato_data=linha["formato_data"] or "%d/%m/%Y",
            mapeamento_colunas=_decodificar_mapeamento(linha["mapeamento_colunas"]),
        )

    def obter_layout(self, id_layout: int) -> Optional[LayoutExportacao]:
        sql = text(
            """SELECT * FROM layouts_exportacao
               WHERE id = :id AND (tenant_id IS NULL OR tenant_id = :tenant_id)"""
        )
        with self._engine.connect() as conn:
            linha = conn.execute(sql, {"id": id_layout, "tenant_id": self.tenant_id}).mappings().first()
        return self._linha_para_layout(linha) if linha else None

    def obter_layout_por_nome(self, nome_layout: str) -> Optional[LayoutExportacao]:
        """Prioriza um layout PRÓPRIO do tenant com esse nome sobre um global de mesmo nome."""
        sql = text(
            """SELECT * FROM layouts_exportacao
               WHERE nome_layout = :nome_layout AND (tenant_id IS NULL OR tenant_id = :tenant_id)
               ORDER BY (tenant_id IS NULL)
               LIMIT 1"""
        )
        with self._engine.connect() as conn:
            linha = conn.execute(sql, {"nome_layout": nome_layout, "tenant_id": self.tenant_id}).mappings().first()
        return self._linha_para_layout(linha) if linha else None

    def adicionar_layout(
        self,
        nome_layout: str,
        formato_arquivo: str,
        mapeamento_colunas: List[Dict[str, str]],
        delimitador: str = ";",
        formato_data: str = "%d/%m/%Y",
    ) -> int:
        nome_layout = (nome_layout or "").strip()
        if not nome_layout:
            raise LayoutInvalidoError("Nome do layout não pode ser vazio.")
        if formato_arquivo not in ("xlsx", "csv", "txt"):
            raise LayoutInvalidoError("formato_arquivo deve ser 'xlsx', 'csv' ou 'txt'.")
        if not mapeamento_colunas:
            raise LayoutInvalidoError("O layout precisa de pelo menos uma coluna mapeada.")

        sql = text(
            """INSERT INTO layouts_exportacao
                   (tenant_id, nome_layout, formato_arquivo, separador, formato_data, mapeamento_colunas, is_padrao)
               VALUES (:tenant_id, :nome_layout, :formato_arquivo, :separador, :formato_data, CAST(:mapeamento AS jsonb), false)
               RETURNING id"""
        )
        try:
            with self._engine.begin() as conn:
                resultado = conn.execute(
                    sql,
                    {
                        "tenant_id": self.tenant_id, "nome_layout": nome_layout, "formato_arquivo": formato_arquivo,
                        "separador": delimitador, "formato_data": formato_data,
                        "mapeamento": json.dumps(mapeamento_colunas, ensure_ascii=False),
                    },
                )
                return resultado.scalar_one()
        except IntegrityError as exc:
            raise LayoutInvalidoError(f"Já existe um layout chamado '{nome_layout}' para esta empresa.") from exc

    def atualizar_layout(self, id_layout: int, **campos) -> None:
        """
        Aceita nome_layout, formato_arquivo, delimitador, formato_data e
        mapeamento_colunas (lista de dicts — convertida para JSONB
        automaticamente). Só atualiza um layout global (tenant_id NULL) ou
        um layout do próprio tenant — nunca um layout de outro tenant.
        """
        campos_validos = {k: v for k, v in campos.items() if k in self._COLUNAS_LAYOUT_ATUALIZAVEIS and v is not None}
        if not campos_validos:
            return
        if campos_validos.get("formato_arquivo") not in (None, "xlsx", "csv", "txt"):
            raise LayoutInvalidoError("formato_arquivo deve ser 'xlsx', 'csv' ou 'txt'.")

        atribuicoes = []
        parametros: Dict[str, object] = {"id": id_layout, "tenant_id": self.tenant_id}
        for chave, valor in campos_validos.items():
            if chave == "delimitador":
                atribuicoes.append("separador = :separador")
                parametros["separador"] = valor
            elif chave == "mapeamento_colunas":
                if not valor:
                    raise LayoutInvalidoError("O layout precisa de pelo menos uma coluna mapeada.")
                atribuicoes.append("mapeamento_colunas = CAST(:mapeamento AS jsonb)")
                parametros["mapeamento"] = json.dumps(valor, ensure_ascii=False)
            else:
                atribuicoes.append(f"{chave} = :{chave}")
                parametros[chave] = valor

        sql = text(
            f"UPDATE layouts_exportacao SET {', '.join(atribuicoes)} "
            "WHERE id = :id AND (tenant_id IS NULL OR tenant_id = :tenant_id)"
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(sql, parametros)
        except IntegrityError as exc:
            raise LayoutInvalidoError(f"Não foi possível atualizar o layout: {exc}") from exc

    def remover_layout(self, id_layout: int) -> None:
        layout = self.obter_layout(id_layout)
        if layout and layout.nome_layout == NOME_LAYOUT_PADRAO:
            raise LayoutInvalidoError(f"O layout padrão '{NOME_LAYOUT_PADRAO}' não pode ser excluído.")
        # WHERE tenant_id = :tenant_id (sem "OR IS NULL") já impede excluir um layout global
        # por engano, mesmo que ele tenha sido renomeado.
        sql = text("DELETE FROM layouts_exportacao WHERE id = :id AND tenant_id = :tenant_id")
        with self._engine.begin() as conn:
            conn.execute(sql, {"id": id_layout, "tenant_id": self.tenant_id})


# --------------------------------------------------------------------------- #
# Regras de classificação "De/Para"
# --------------------------------------------------------------------------- #

class RegrasDB:
    """
    Regras de classificação (tabela regras_de_para), isoladas por
    tenant_id. Cada regra pode ainda ser restrita a um CNPJ e/ou a uma
    conta de Banco específicos — deixados em branco, valem como coringa
    para qualquer empresa/banco DENTRO do mesmo tenant (nunca atravessam
    para outro tenant). Não guarda conexão persistente (ver BancoDados).
    """

    _COLUNAS_ATUALIZAVEIS = (
        "cnpj_empresa", "codigo_banco", "padrao_texto", "tipo_padrao",
        "conta_debito", "conta_credito", "numero_historico", "formato_variavel",
    )

    def __init__(self, tenant_id: Optional[str] = None):
        self.tenant_id = tenant_id or _obter_tenant_id()
        self._engine = obter_engine()
        self._cache_regras: Optional[list] = None  # carregado sob demanda — ver _carregar_cache()

    def __enter__(self) -> "RegrasDB":
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def fechar(self) -> None:
        """Sem efeito — mantido por compatibilidade; o pool de conexões cuida do ciclo de vida."""

    def _carregar_cache(self) -> None:
        """
        Carrega todas as regras deste tenant em memória na primeira chamada
        de buscar_regra() — evita uma consulta de rede ao Postgres por
        lançamento (um extrato com centenas de linhas faria centenas de
        idas e vindas ao Supabase, um por um, o que é visivelmente lento).
        Uma vez cacheado, buscar_regra() casa os padrões em Python.
        """
        if self._cache_regras is not None:
            return
        sql = text("SELECT * FROM regras_de_para WHERE tenant_id = :tenant_id ORDER BY id")
        with self._engine.connect() as conn:
            self._cache_regras = list(conn.execute(sql, {"tenant_id": self.tenant_id}).mappings().all())

    def invalidar_cache(self) -> None:
        """Força recarregar as regras do banco na próxima busca — chame depois de adicionar/editar/excluir regras."""
        self._cache_regras = None

    def atribuir_cnpj_em_massa(self, cnpj_empresa: str) -> int:
        """
        Atribui `cnpj_empresa` a todas as regras deste tenant que ainda
        estão sem CNPJ vinculado (coringa) — útil para regras salvas antes
        de uma Empresa ter sido selecionada na barra lateral. Devolve
        quantas regras foram atualizadas.
        """
        cnpj_empresa = (cnpj_empresa or "").strip()
        if not cnpj_empresa:
            raise RegraInvalidaError("Informe um CNPJ para atribuir às regras.")
        sql = text(
            """UPDATE regras_de_para SET cnpj_empresa = :cnpj_empresa
               WHERE tenant_id = :tenant_id AND (cnpj_empresa IS NULL OR cnpj_empresa = '')"""
        )
        with self._engine.begin() as conn:
            resultado = conn.execute(sql, {"cnpj_empresa": cnpj_empresa, "tenant_id": self.tenant_id})
            linhas_afetadas = resultado.rowcount
        self.invalidar_cache()
        return linhas_afetadas

    def adicionar_regra(
        self,
        padrao_texto: str,
        conta_debito: str = "",
        conta_credito: str = "",
        numero_historico: str = "",
        formato_variavel: str = "",
        cnpj_empresa: Optional[str] = None,
        codigo_banco: Optional[str] = None,
        tipo_padrao: str = "contém",
    ) -> int:
        """Cadastra uma regra fixa. tipo_padrao: 'contém' (ILIKE) ou 'regex' (~*, case-insensitive)."""
        if not cnpj_empresa or not str(cnpj_empresa).strip():
            raise RegraInvalidaError(
                "CNPJ da empresa é obrigatório — selecione uma Empresa antes de salvar a regra "
                "(evita regras 'coringa' que valeriam para qualquer CNPJ do tenant sem querer)."
            )
        if tipo_padrao not in ("contém", "regex"):
            raise RegraInvalidaError("tipo_padrao deve ser 'contém' ou 'regex'.")
        if tipo_padrao == "regex":
            try:
                re.compile(padrao_texto)
            except re.error as exc:
                raise RegraInvalidaError(f"Padrão regex inválido: '{padrao_texto}' ({exc})") from exc
        if not padrao_texto or not padrao_texto.strip():
            raise RegraInvalidaError("padrao_texto não pode ser vazio.")

        sql = text(
            """INSERT INTO regras_de_para
                   (tenant_id, cnpj_empresa, codigo_banco, padrao_texto, tipo_padrao,
                    conta_debito, conta_credito, numero_historico, formato_variavel)
               VALUES (:tenant_id, :cnpj_empresa, :codigo_banco, :padrao_texto, :tipo_padrao,
                       :conta_debito, :conta_credito, :numero_historico, :formato_variavel)
               RETURNING id"""
        )
        with self._engine.begin() as conn:
            resultado = conn.execute(
                sql,
                {
                    "tenant_id": self.tenant_id, "cnpj_empresa": cnpj_empresa, "codigo_banco": codigo_banco,
                    "padrao_texto": padrao_texto.strip(), "tipo_padrao": tipo_padrao,
                    "conta_debito": conta_debito, "conta_credito": conta_credito,
                    "numero_historico": numero_historico, "formato_variavel": formato_variavel,
                },
            )
            id_nova = resultado.scalar_one()
        self.invalidar_cache()
        return id_nova

    def adicionar_regra_contrapartida(
        self,
        padrao_texto: str,
        conta_contrapartida: str,
        conta_banco: str,
        numero_historico: str = "",
        formato_variavel: str = "",
        cnpj_empresa: Optional[str] = None,
        codigo_banco: Optional[str] = None,
        tipo_padrao: str = "contém",
    ) -> int:
        """
        Atalho para cadastrar uma regra informando apenas a conta de
        contrapartida: o lado correto (débito/crédito) é resolvido em tempo
        de classificação, conforme o tipo (entrada/saída) de cada lançamento.
        """
        return self.adicionar_regra(
            padrao_texto=padrao_texto,
            conta_debito=conta_banco,
            conta_credito=conta_contrapartida,
            numero_historico=numero_historico,
            formato_variavel=formato_variavel,
            cnpj_empresa=cnpj_empresa,
            codigo_banco=codigo_banco,
            tipo_padrao=tipo_padrao,
        )

    def remover_regra(self, id_regra: int) -> None:
        sql = text("DELETE FROM regras_de_para WHERE id = :id AND tenant_id = :tenant_id")
        with self._engine.begin() as conn:
            conn.execute(sql, {"id": id_regra, "tenant_id": self.tenant_id})
        self.invalidar_cache()

    def atualizar_regra(self, id_regra: int, **campos) -> None:
        campos_validos = {k: v for k, v in campos.items() if k in self._COLUNAS_ATUALIZAVEIS}
        if not campos_validos:
            return
        if campos_validos.get("tipo_padrao") == "regex":
            try:
                re.compile(campos_validos.get("padrao_texto") or "")
            except re.error as exc:
                raise RegraInvalidaError(f"Padrão regex inválido: {exc}") from exc

        set_clause = ", ".join(f"{c} = :{c}" for c in campos_validos)
        sql = text(f"UPDATE regras_de_para SET {set_clause} WHERE id = :id AND tenant_id = :tenant_id")
        with self._engine.begin() as conn:
            conn.execute(sql, {**campos_validos, "id": id_regra, "tenant_id": self.tenant_id})
        self.invalidar_cache()

    def listar_regras(self, cnpj_empresa: Optional[str] = None) -> pd.DataFrame:
        """
        Lista as regras deste tenant. Com `cnpj_empresa` informado, mostra
        só as regras vinculadas a esse CNPJ mais as regras coringa (sem
        CNPJ vinculado) — mesmo critério usado por buscar_regra().
        """
        if cnpj_empresa:
            sql = text(
                """SELECT * FROM regras_de_para
                   WHERE tenant_id = :tenant_id
                     AND (cnpj_empresa = :cnpj_empresa OR cnpj_empresa IS NULL OR cnpj_empresa = '')
                   ORDER BY id"""
            )
            params = {"tenant_id": self.tenant_id, "cnpj_empresa": cnpj_empresa}
        else:
            sql = text("SELECT * FROM regras_de_para WHERE tenant_id = :tenant_id ORDER BY id")
            params = {"tenant_id": self.tenant_id}
        return pd.read_sql_query(sql, self._engine, params=params)

    def buscar_regra(
        self, historico: str, cnpj_empresa: Optional[str] = None, codigo_banco: Optional[str] = None
    ):
        """
        Retorna a primeira regra DESTE tenant cujo padrão bate com o
        histórico informado, entre as regras compatíveis com cnpj_empresa/
        codigo_banco (uma regra com esses campos em branco vale como
        coringa, mas só dentro do mesmo tenant). Regras mais específicas
        (CNPJ e Banco preenchidos e batendo) têm prioridade sobre as mais
        genéricas. Padrões 'contém' comparam substring sem diferenciar
        maiúsculas/minúsculas (equivalente ao ILIKE do Postgres); padrões
        'regex' usam re.search (case-insensitive).

        Casa os padrões em Python contra um cache de todas as regras do
        tenant, carregado uma única vez por instância (ver _carregar_cache)
        — evita uma consulta de rede ao Postgres por lançamento, essencial
        para não deixar a classificação de um extrato inteiro lenta.
        """
        self._carregar_cache()

        candidatos = [
            regra
            for regra in self._cache_regras
            if (not regra["cnpj_empresa"] or regra["cnpj_empresa"] == cnpj_empresa)
            and (not regra["codigo_banco"] or regra["codigo_banco"] == codigo_banco)
        ]
        candidatos.sort(
            key=lambda r: (0 if r["cnpj_empresa"] else 1) + (0 if r["codigo_banco"] else 1)
        )

        historico_norm = (historico or "").lower()
        for regra in candidatos:
            padrao = regra["padrao_texto"] or ""
            if regra["tipo_padrao"] == "regex":
                try:
                    if re.search(padrao, historico or "", flags=re.IGNORECASE):
                        return regra
                except re.error:
                    continue  # regra mal cadastrada não pode quebrar a classificação inteira
            elif padrao and padrao.lower() in historico_norm:
                return regra
        return None
