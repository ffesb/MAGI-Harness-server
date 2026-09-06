from typing import Any, Dict, List, Optional
from magi.council.voter import DeliberationOutcome, MindVerdict


class PolicyEngine:
    @staticmethod
    def evaluate(
        action_id: str,
        tool_name: str,
        tier: int,
        magi_enabled: bool,
        verdicts: Dict[str, MindVerdict],
    ) -> DeliberationOutcome:
        # Tier 0: Solo lectura (nunca requiere voto)
        if tier == 0:
            return DeliberationOutcome(
                action_id=action_id,
                tool_name=tool_name,
                tier=0,
                magi_enabled=magi_enabled,
                verdicts=verdicts,
                approved_count=0,
                rejected_count=0,
                abstained_count=0,
                consensus_type="READ_ONLY_AUTO_PASS",
                passed=True,
                requires_human_confirmation=False,
                explanation="Operación informativa / solo lectura permitida automáticamente.",
            )

        # Si MAGI está desactivado (/disable)
        if not magi_enabled:
            if tier <= 1:
                return DeliberationOutcome(
                    action_id=action_id,
                    tool_name=tool_name,
                    tier=tier,
                    magi_enabled=False,
                    verdicts=verdicts,
                    approved_count=0,
                    rejected_count=0,
                    abstained_count=0,
                    consensus_type="BYPASSED_DISABLED",
                    passed=True,
                    requires_human_confirmation=False,
                    explanation="MAGI desactivado (/disable): acción Tier 1 aprobada directamente.",
                )
            else:
                # Tier 2 o 3: ¡El freno humano NUNCA se desactiva!
                return DeliberationOutcome(
                    action_id=action_id,
                    tool_name=tool_name,
                    tier=tier,
                    magi_enabled=False,
                    verdicts=verdicts,
                    approved_count=0,
                    rejected_count=0,
                    abstained_count=0,
                    consensus_type="BYPASSED_DISABLED_NEEDS_HUMAN",
                    passed=True,
                    requires_human_confirmation=True,
                    explanation="MAGI desactivado, pero acción crítica Tier 2/3 requiere confirmación humana obligatoria.",
                )

        # MAGI ACTIVO: Computar votos de las 3 mentes
        approved = sum(1 for v in verdicts.values() if v.vote == "approve")
        rejected = sum(1 for v in verdicts.values() if v.vote == "reject")
        abstained = sum(1 for v in verdicts.values() if v.vote == "abstain")

        # Tabla de votación (Sección 9 y 10)
        # 1. Consenso unánime (3-0)
        if approved == 3:
            consensus_type = "UNANIMOUS_CONSENSUS"
            passed = True
            # Tier 2 y 3 requieren confirmación humana aun con 3-0
            requires_human = tier in (2, 3)
            explanation = (
                "Consenso unánime absoluto (3-0): MELCHIOR, BALTHASAR y CASPER aprueban la acción."
            )
            if requires_human:
                explanation += " Por ser Tier crítico, se requiere confirmación humana explícita."

        # 2. Aprobación mayoritaria (2-1 o 2-0-1)
        elif approved == 2:
            if tier == 1:
                consensus_type = "MAJORITY_APPROVAL"
                passed = True
                requires_human = False
                explanation = "Aprobación mayoritaria (2-1). Se autoriza ejecución de Tier 1."
            else:
                consensus_type = "BLOCKED_DISAGREEMENT"
                passed = False
                requires_human = False
                explanation = (
                    f"Bloqueo por desacuerdo: Tier {tier} exige unanimidad 3-0. La mayoría simple 2-1 no es suficiente."
                )

        # 3. Rechazo mayoritario o unánime (0-3, 1-2)
        elif rejected >= 2:
            consensus_type = "REJECTED"
            passed = False
            requires_human = False
            explanation = f"Rechazado por el consejo MAGI ({rejected} votos en contra)."

        # 4. Cualquier otro caso (empates con abstenciones, etc.)
        else:
            consensus_type = "REJECTED"
            passed = False
            requires_human = False
            explanation = "No se alcanzó el quórum mínimo requerido para autorizar la acción."

        return DeliberationOutcome(
            action_id=action_id,
            tool_name=tool_name,
            tier=tier,
            magi_enabled=True,
            verdicts=verdicts,
            approved_count=approved,
            rejected_count=rejected,
            abstained_count=abstained,
            consensus_type=consensus_type,
            passed=passed,
            requires_human_confirmation=requires_human,
            explanation=explanation,
        )
