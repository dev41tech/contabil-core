"""Serviço de Plano de Contas.

Regras de negócio:
- Código único por empresa — não pode repetir dentro do mesmo plano.
- Código do filho deve começar com o código do pai (ex: pai=1, filho=1.1).
- Nível máximo: 5 (ex: 1.1.1.1.1). Além disso fica difícil de operar.
- Não é possível deletar (soft) uma conta que tenha filhos ativos.
- Não é possível deletar uma conta que esteja referenciada em regras ativas.
- Atualizar código não é permitido — código é imutável após criação (muda
  a posição hierárquica e quebraria referências em regras).
- A árvore retorna apenas contas não-deletadas, em ordem por código.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import (
    LancamentoCartao,
    PlanoConta,
    PlanoContaFaixaTipo,
    RegistroContabil,
    Regra,
)
from src.domain.auditoria import registrar_auditoria
from src.schemas.plano_contas import (
    FaixaTipoItem,
    FaixaTipoResponse,
    FaixasTipoListResponse,
    PlanoContaCreate,
    PlanoContaExclusaoBloqueada,
    PlanoContaExclusaoLoteResultado,
    PlanoContaListResponse,
    PlanoContaNode,
    PlanoContaResponse,
    PlanoContaTreeResponse,
    PlanoContaUpdate,
    codigo_para_tupla,
)

logger = structlog.get_logger(__name__)

_NAO_ENCONTRADA = "Conta contábil não encontrada."
_NIVEL_MAXIMO = 5


class PlanoContaService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    # ── Consultas ─────────────────────────────────────────────────────────────

    async def listar(self) -> PlanoContaListResponse:
        """Lista todas as contas em ordem por código."""
        q = (
            select(PlanoConta)
            .where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
            )
            .order_by(PlanoConta.codigo)
        )
        rows = (await self._db.execute(q)).scalars().all()
        total = len(rows)
        return PlanoContaListResponse(
            items=[PlanoContaResponse.model_validate(r) for r in rows],
            total=total,
        )

    async def arvore(self) -> PlanoContaTreeResponse:
        """Retorna o plano de contas em estrutura de árvore (raízes + filhos aninhados)."""
        # Carrega todas as contas com filhos em uma única query
        q = (
            select(PlanoConta)
            .where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
                PlanoConta.pai_id == None,  # apenas raízes
            )
            .options(selectinload(PlanoConta.filhos).selectinload(PlanoConta.filhos)
                     .selectinload(PlanoConta.filhos).selectinload(PlanoConta.filhos))
            .order_by(PlanoConta.codigo)
        )
        raizes = (await self._db.execute(q)).scalars().all()

        total_q = select(func.count()).where(
            PlanoConta.empresa_id == self._empresa_id,
            PlanoConta.deleted_at == None,
        )
        total = (await self._db.execute(total_q)).scalar_one()

        return PlanoContaTreeResponse(
            tree=[_to_node(r) for r in raizes],
            total=total,
        )

    async def obter(self, conta_id: UUID) -> PlanoContaResponse:
        conta = await self._get_or_404(conta_id)
        return PlanoContaResponse.model_validate(conta)

    # ── Mutações ──────────────────────────────────────────────────────────────

    async def criar(self, data: PlanoContaCreate) -> PlanoContaResponse:
        # Verifica código duplicado
        await self._assert_codigo_livre(data.codigo)

        # Valida pai se informado
        if data.pai_id:
            pai = await self._get_or_404(data.pai_id)
            self._assert_codigo_filho_valido(pai.codigo, data.codigo)
            self._assert_nivel_valido(pai.codigo)

        conta_numero = data.conta_numero
        if conta_numero is None:
            conta_numero = await self._proximo_conta_numero()

        conta = PlanoConta(
            empresa_id=self._empresa_id,
            conta_numero=conta_numero,
            codigo=data.codigo,
            descricao=data.descricao,
            tipo=data.tipo,
            tipo_sa=data.tipo_sa,
            pai_id=data.pai_id,
        )
        self._db.add(conta)
        await self._db.flush()

        # Auto-promove o pai para Sintética quando ganha o primeiro filho
        if data.pai_id:
            pai = await self._get_or_404(data.pai_id)
            if pai.tipo_sa != "S":
                pai.tipo_sa = "S"
                await self._db.flush()

        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="plano_conta.criada",
            entidade="plano_conta",
            entidade_id=conta.id,
            dados_depois=_snapshot_conta(conta),
        )

        logger.info(
            "plano_conta.criada",
            conta_id=str(conta.id),
            empresa_id=str(self._empresa_id),
            codigo=conta.codigo,
        )
        return PlanoContaResponse.model_validate(conta)

    async def atualizar(self, conta_id: UUID, data: PlanoContaUpdate) -> PlanoContaResponse:
        conta = await self._get_or_404(conta_id)
        antes = _snapshot_conta(conta)

        altera_natureza = (
            (data.tipo is not None and data.tipo != conta.tipo)
            or (data.tipo_sa is not None and data.tipo_sa != conta.tipo_sa)
        )
        if altera_natureza and await self._tem_movimentacao(conta_id):
            raise ConflictError(
                message=(
                    "O tipo e a classificação sintética/analítica da conta não podem ser "
                    "alterados após a existência de movimentações contábeis."
                )
            )

        if data.conta_numero is not None:
            conta.conta_numero = data.conta_numero
        if data.codigo is not None and data.codigo != conta.codigo:
            raise ConflictError(
                message="O código da conta é imutável após a criação. Crie uma nova conta."
            )
        if data.descricao is not None:
            conta.descricao = data.descricao
        if data.tipo is not None:
            conta.tipo = data.tipo
        if data.tipo_sa is not None:
            if data.tipo_sa == "A":
                filhos_q = select(func.count()).where(
                    PlanoConta.pai_id == conta_id,
                    PlanoConta.deleted_at == None,
                )
                if (await self._db.execute(filhos_q)).scalar_one() > 0:
                    raise ConflictError(
                        message="Conta com subcontas não pode ser alterada para Analítica."
                    )
            conta.tipo_sa = data.tipo_sa

        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="plano_conta.atualizada",
            entidade="plano_conta",
            entidade_id=conta.id,
            dados_antes=antes,
            dados_depois=_snapshot_conta(conta),
        )
        logger.info("plano_conta.atualizada", conta_id=str(conta_id))
        return PlanoContaResponse.model_validate(conta)

    async def remover(self, conta_id: UUID) -> None:
        """Soft delete — bloqueia se houver filhos ou referências financeiras."""
        conta = await self._get_or_404(conta_id)
        antes = _snapshot_conta(conta)

        # Verifica filhos ativos
        filhos_q = select(func.count()).where(
            PlanoConta.pai_id == conta_id,
            PlanoConta.deleted_at == None,
        )
        n_filhos = (await self._db.execute(filhos_q)).scalar_one()
        if n_filhos > 0:
            raise ConflictError(
                message=(
                    f"A conta '{conta.codigo} — {conta.descricao}' possui {n_filhos} "
                    "subconta(s). Remova-as antes de remover a conta pai."
                )
            )

        # Verifica referências em regras
        regras_q = select(func.count()).where(
            Regra.conta_id == conta_id,
            Regra.empresa_id == self._empresa_id,
            Regra.ativa == True,
        )
        n_regras = (await self._db.execute(regras_q)).scalar_one()
        if n_regras > 0:
            raise ConflictError(
                message=(
                    f"A conta '{conta.codigo} — {conta.descricao}' está referenciada "
                    f"em {n_regras} regra(s) ativa(s). Remova as regras antes."
                )
            )

        if await self._tem_movimentacao(conta_id):
            raise ConflictError(
                message=(
                    f"A conta '{conta.codigo} — {conta.descricao}' possui movimentações "
                    "contábeis e não pode ser removida."
                )
            )

        from datetime import UTC, datetime
        conta.deleted_at = datetime.now(UTC)
        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="plano_conta.removida",
            entidade="plano_conta",
            entidade_id=conta.id,
            dados_antes=antes,
            dados_depois=_snapshot_conta(conta),
        )
        logger.info("plano_conta.removida", conta_id=str(conta_id), codigo=conta.codigo)

    async def remover_lote(
        self, ids: list[UUID], *, todas: bool = False
    ) -> PlanoContaExclusaoLoteResultado:
        """Remove várias contas (soft delete), isolando falhas por conta.

        Processa do nível mais profundo para o mais raso, para que a remoção de
        uma conta pai selecionada junto com seus filhos funcione numa única
        chamada. Cada conta passa pelas mesmas validações de `remover()`
        (filhos ativos, regras, movimentações) — contas bloqueadas não
        interrompem o restante do lote.
        """
        if todas:
            q = select(PlanoConta.id, PlanoConta.codigo).where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
            )
            alvo = list((await self._db.execute(q)).all())
        else:
            q = select(PlanoConta.id, PlanoConta.codigo).where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
                PlanoConta.id.in_(ids),
            )
            alvo = list((await self._db.execute(q)).all())

        # Filhos antes de pais: ordena por profundidade do código, decrescente.
        alvo.sort(key=lambda item: (item.codigo.count("."), item.codigo), reverse=True)

        removidas = 0
        bloqueadas: list[PlanoContaExclusaoBloqueada] = []
        for conta_id, codigo in alvo:
            try:
                await self.remover(conta_id)
                removidas += 1
            except (ConflictError, NotFoundError) as exc:
                bloqueadas.append(
                    PlanoContaExclusaoBloqueada(id=conta_id, codigo=codigo, erro=exc.message)
                )

        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="plano_conta.exclusao_lote",
            entidade="plano_conta",
            entidade_id=self._empresa_id,
            dados_depois={
                "removidas": removidas,
                "bloqueadas": [b.codigo for b in bloqueadas],
                "todas": todas,
            },
        )
        logger.info(
            "plano_conta.exclusao_lote",
            empresa_id=str(self._empresa_id),
            removidas=removidas,
            bloqueadas=len(bloqueadas),
        )
        return PlanoContaExclusaoLoteResultado(removidas=removidas, bloqueadas=bloqueadas)

    async def importar_lote(self, rows: list[dict]) -> "ImportacaoPlanoResult":
        """Importa contas em duas fases, preservando hierarquia e isolando falhas por linha."""
        from src.api.v1.plano_contas import ImportacaoLinhaErro, ImportacaoPlanoResult

        importadas = 0
        duplicadas = 0
        erros: list[ImportacaoLinhaErro] = []
        faixas_classificacao = await self._faixas_para_classificacao()

        candidatos: dict[str, tuple[PlanoContaCreate, int]] = {}
        for row in rows:
            linha = row.get("_linha", 0)
            codigo = row.get("codigo", "").strip()
            descricao = row.get("descricao", "").strip()
            tipo = row.get("tipo", "").strip().lower()
            tipo_sa = (row.get("tipo_sa", "A") or "A").strip().upper()
            conta_num_raw = row.get("conta_numero", row.get("conta", ""))
            conta_numero: int | None = None
            if conta_num_raw:
                try:
                    # openpyxl às vezes entrega a célula numérica como float
                    # ("1507.0"): int() direto rejeita o ponto e o número
                    # cai silenciosamente pro fallback de auto-numeração.
                    conta_numero = int(float(str(conta_num_raw).strip()))
                except (ValueError, TypeError):
                    pass

            if not codigo:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=None, erro="Código ausente."))
                continue
            if not descricao:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=codigo, erro="Descrição ausente."))
                continue
            if not tipo:
                # Nunca adivinhar o tipo contábil sem uma regra explícita: um
                # "despesa" default errado se propaga silenciosamente pros
                # relatórios do cliente. Faixas configuradas pela própria
                # empresa (não heurística do sistema) são a única exceção.
                tipo_inferido = self._classificar_por_faixa(codigo, faixas_classificacao)
                if tipo_inferido is None:
                    erros.append(
                        ImportacaoLinhaErro(
                            linha=linha,
                            codigo=codigo,
                            erro=(
                                "Tipo ausente e código não corresponde a nenhuma faixa "
                                "de classificação configurada. Inclua a coluna \"Tipo\" "
                                "na planilha, informe manualmente após importar, ou "
                                "configure as faixas de classificação da empresa."
                            ),
                        )
                    )
                    continue
                tipo = tipo_inferido

            try:
                data = PlanoContaCreate(
                    conta_numero=conta_numero,
                    codigo=codigo,
                    descricao=descricao,
                    tipo=tipo,
                    tipo_sa=tipo_sa,
                )
            except Exception as e:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=codigo, erro=str(e)))
                continue

            if codigo in candidatos:
                duplicadas += 1
                continue
            candidatos[codigo] = (data, linha)

        existentes_q = select(PlanoConta).where(
            PlanoConta.empresa_id == self._empresa_id,
            PlanoConta.deleted_at == None,
        )
        existentes = {
            conta.codigo: conta
            for conta in (await self._db.execute(existentes_q)).scalars().all()
        }

        # Valida previamente se todo código hierárquico tem seus ancestrais no
        # banco ou no próprio lote. Assim nenhuma conta filha vira raiz por engano.
        validos: dict[str, tuple[PlanoContaCreate, int]] = {}
        codigos_disponiveis = set(existentes) | set(candidatos)
        for codigo, item in candidatos.items():
            if codigo in existentes:
                duplicadas += 1
                continue
            partes = codigo.split(".")
            if len(partes) > _NIVEL_MAXIMO:
                erros.append(
                    ImportacaoLinhaErro(
                        linha=item[1],
                        codigo=codigo,
                        erro=f"Nível máximo de hierarquia excedido ({_NIVEL_MAXIMO}).",
                    )
                )
                continue
            ancestrais = [".".join(partes[:nivel]) for nivel in range(1, len(partes))]
            ausente = next(
                (ancestral for ancestral in ancestrais if ancestral not in codigos_disponiveis),
                None,
            )
            if ausente:
                erros.append(
                    ImportacaoLinhaErro(
                        linha=item[1],
                        codigo=codigo,
                        erro=f"Conta ancestral '{ausente}' não encontrada no plano ou no lote.",
                    )
                )
                continue
            validos[codigo] = item

        proximo_numero = await self._proximo_conta_numero()

        criadas: dict[str, PlanoConta] = {}
        for codigo in sorted(validos, key=lambda value: (value.count("."), value)):
            data, linha = validos[codigo]
            conta_numero = data.conta_numero
            if conta_numero is None:
                conta_numero = proximo_numero
                proximo_numero += 1
            conta = PlanoConta(
                empresa_id=self._empresa_id,
                conta_numero=conta_numero,
                codigo=data.codigo,
                descricao=data.descricao,
                tipo=data.tipo,
                tipo_sa=data.tipo_sa,
                pai_id=None,
            )
            try:
                async with self._db.begin_nested():
                    self._db.add(conta)
                    await self._db.flush()
                criadas[codigo] = conta
                importadas += 1
            except Exception as exc:
                erros.append(
                    ImportacaoLinhaErro(linha=linha, codigo=codigo, erro=str(exc))
                )

        # Segunda fase: agora todos os IDs existem e a relação pai/filho pode ser
        # montada inclusive quando o pai apareceu depois do filho na planilha.
        todas = existentes | criadas
        for codigo in sorted(list(criadas), key=lambda value: (value.count("."), value)):
            conta = criadas[codigo]
            if "." not in codigo:
                continue
            pai = todas.get(codigo.rsplit(".", 1)[0])
            if pai is None:  # pai pode ter falhado ao inserir no savepoint
                erros.append(
                    ImportacaoLinhaErro(
                        linha=validos[codigo][1],
                        codigo=codigo,
                        erro="Conta pai não pôde ser importada; hierarquia não foi criada.",
                    )
                )
                await self._db.delete(conta)
                criadas.pop(codigo)
                todas.pop(codigo)
                importadas -= 1
                continue
            conta.pai_id = pai.id
            pai.tipo_sa = "S"

        await self._db.flush()

        if criadas:
            await registrar_auditoria(
                self._db,
                empresa_id=self._empresa_id,
                acao="plano_conta.importacao",
                entidade="plano_conta",
                entidade_id=self._empresa_id,
                dados_depois={
                    "quantidade": len(criadas),
                    "contas": [
                        {"id": conta.id, "codigo": conta.codigo}
                        for conta in criadas.values()
                    ],
                },
            )

        return ImportacaoPlanoResult(importadas=importadas, duplicadas=duplicadas, erros=erros)

    # ── Faixas de classificação (código → tipo, configurada por empresa) ──────

    async def listar_faixas_tipo(self) -> FaixasTipoListResponse:
        q = (
            select(PlanoContaFaixaTipo)
            .where(
                PlanoContaFaixaTipo.empresa_id == self._empresa_id,
                PlanoContaFaixaTipo.deleted_at == None,
            )
            .order_by(PlanoContaFaixaTipo.tipo, PlanoContaFaixaTipo.codigo_de)
        )
        faixas = (await self._db.execute(q)).scalars().all()
        return FaixasTipoListResponse(
            faixas=[FaixaTipoResponse.model_validate(f) for f in faixas]
        )

    async def salvar_faixas_tipo(self, faixas: list[FaixaTipoItem]) -> FaixasTipoListResponse:
        """Substitui o conjunto inteiro de faixas da empresa — não é PATCH
        incremental. Rejeita se alguma faixa de um tipo sobrepõe a de outro
        tipo (ambiguidade real: mesmo código classificaria em dois lugares)."""
        self._valida_faixas_sem_sobreposicao(faixas)

        antigas_q = select(PlanoContaFaixaTipo).where(
            PlanoContaFaixaTipo.empresa_id == self._empresa_id,
            PlanoContaFaixaTipo.deleted_at == None,
        )
        antigas = (await self._db.execute(antigas_q)).scalars().all()
        agora = datetime.now(UTC)
        for antiga in antigas:
            antiga.deleted_at = agora

        novas = [
            PlanoContaFaixaTipo(
                empresa_id=self._empresa_id,
                tipo=item.tipo,
                codigo_de=item.codigo_de,
                codigo_ate=item.codigo_ate,
            )
            for item in faixas
        ]
        self._db.add_all(novas)
        await self._db.flush()

        return FaixasTipoListResponse(
            faixas=[FaixaTipoResponse.model_validate(f) for f in novas]
        )

    @staticmethod
    def _valida_faixas_sem_sobreposicao(faixas: list[FaixaTipoItem]) -> None:
        anotadas = [
            (item, codigo_para_tupla(item.codigo_de), codigo_para_tupla(item.codigo_ate))
            for item in faixas
        ]
        for i, (item_a, de_a, ate_a) in enumerate(anotadas):
            for item_b, de_b, ate_b in anotadas[i + 1:]:
                if item_a.tipo == item_b.tipo:
                    continue  # sobreposição dentro do mesmo tipo é inofensiva
                if de_a <= ate_b and de_b <= ate_a:
                    raise ValidationError(
                        message=(
                            f'Faixa "{item_a.codigo_de} a {item_a.codigo_ate}" '
                            f"({item_a.tipo}) sobrepõe a faixa "
                            f'"{item_b.codigo_de} a {item_b.codigo_ate}" ({item_b.tipo}). '
                            "Ajuste os limites antes de salvar."
                        )
                    )

    async def _faixas_para_classificacao(
        self,
    ) -> list[tuple[str, tuple[int, ...], tuple[int, ...]]]:
        q = select(PlanoContaFaixaTipo).where(
            PlanoContaFaixaTipo.empresa_id == self._empresa_id,
            PlanoContaFaixaTipo.deleted_at == None,
        )
        faixas = (await self._db.execute(q)).scalars().all()
        return [
            (f.tipo, codigo_para_tupla(f.codigo_de), codigo_para_tupla(f.codigo_ate))
            for f in faixas
        ]

    @staticmethod
    def _classificar_por_faixa(
        codigo: str, faixas: list[tuple[str, tuple[int, ...], tuple[int, ...]]]
    ) -> str | None:
        try:
            codigo_tupla = codigo_para_tupla(codigo)
        except ValueError:
            return None
        for tipo, de, ate in faixas:
            if de <= codigo_tupla <= ate:
                return tipo
        return None

    # ── Validações privadas ───────────────────────────────────────────────────

    async def _get_or_404(self, conta_id: UUID) -> PlanoConta:
        result = await self._db.execute(
            select(PlanoConta).where(
                PlanoConta.id == conta_id,
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
            )
        )
        conta = result.scalar_one_or_none()
        if not conta:
            raise NotFoundError(message=_NAO_ENCONTRADA)
        return conta

    async def _assert_codigo_livre(self, codigo: str) -> None:
        existing = await self._db.execute(
            select(PlanoConta).where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.codigo == codigo,
                PlanoConta.deleted_at == None,
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError(message=f"Já existe uma conta com o código '{codigo}'.")

    async def _proximo_conta_numero(self) -> int:
        """Próximo número sequencial livre para `conta_numero` nesta empresa."""
        q = select(func.max(PlanoConta.conta_numero)).where(
            PlanoConta.empresa_id == self._empresa_id,
        )
        maximo = (await self._db.execute(q)).scalar_one_or_none()
        return (maximo or 0) + 1

    async def _tem_movimentacao(self, conta_id: UUID) -> bool:
        registros_q = select(func.count()).where(
            RegistroContabil.empresa_id == self._empresa_id,
            RegistroContabil.conta_id == conta_id,
        )
        if (await self._db.execute(registros_q)).scalar_one() > 0:
            return True
        cartoes_q = select(func.count()).where(
            LancamentoCartao.empresa_id == self._empresa_id,
            LancamentoCartao.conta_id == conta_id,
        )
        return (await self._db.execute(cartoes_q)).scalar_one() > 0

    def _assert_codigo_filho_valido(self, codigo_pai: str, codigo_filho: str) -> None:
        """O código do filho deve ter o código do pai como prefixo."""
        if not codigo_filho.startswith(codigo_pai + "."):
            raise ValidationError(
                message=(
                    f"O código '{codigo_filho}' não é filho do código '{codigo_pai}'. "
                    f"O código filho deve começar com '{codigo_pai}.' "
                    f"(ex: {codigo_pai}.1)."
                )
            )

    def _assert_nivel_valido(self, codigo_pai: str) -> None:
        nivel_pai = len(codigo_pai.split("."))
        if nivel_pai >= _NIVEL_MAXIMO:
            raise ValidationError(
                message=(
                    f"Nível máximo de hierarquia atingido ({_NIVEL_MAXIMO}). "
                    f"A conta '{codigo_pai}' já está no nível {nivel_pai}."
                )
            )


# ── Helpers de conversão ──────────────────────────────────────────────────────


def _to_node(conta: PlanoConta) -> PlanoContaNode:
    """Converte recursivamente um PlanoConta (com filhos carregados) em PlanoContaNode."""
    filhos_ordenados = sorted(
        [f for f in conta.filhos if f.deleted_at is None],
        key=lambda f: f.codigo,
    )
    return PlanoContaNode(
        id=conta.id,
        empresa_id=conta.empresa_id,
        codigo=conta.codigo,
        descricao=conta.descricao,
        tipo=conta.tipo,
        tipo_sa=conta.tipo_sa,
        pai_id=conta.pai_id,
        nivel=conta.nivel,
        filhos=[_to_node(f) for f in filhos_ordenados],
    )


def _snapshot_conta(conta: PlanoConta) -> dict[str, object]:
    return {
        "conta_numero": conta.conta_numero,
        "codigo": conta.codigo,
        "descricao": conta.descricao,
        "tipo": conta.tipo,
        "tipo_sa": conta.tipo_sa,
        "pai_id": conta.pai_id,
        "deleted_at": conta.deleted_at,
    }
