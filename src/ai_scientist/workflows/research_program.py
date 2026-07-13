from __future__ import annotations

import asyncio
from typing import Any

from ..agents import (
    AnchorDirectorAgent,
    ExpansionDirectorAgent,
    ProgramEvaluatorAAgent,
    ProgramEvaluatorBAgent,
    ResearchProgramComposerAgent,
)
from ..artifacts import ArtifactStore
from ..config import (
    Settings,
    hypothesis_rounds,
    repair_attempts,
    repair_budget_exhausted,
)
from ..program_validation import (
    EVALUATOR_A_GATES,
    EVALUATOR_B_GATES,
    TRACE_EVALUATOR_A_GATES,
    build_research_contract_from_program,
    compute_claim_promotions,
    filter_promotions_by_dependencies,
    validate_claim_dependencies,
    validate_claim_director,
    validate_claim_evaluator,
    validate_program_composition,
)
from ..runtime import DeadlinePolicy, RuntimePhase
from ..schemas import (
    ClaimDirectorOutput,
    ClaimErrorNote,
    ClaimEvaluatorReport,
    DirectorRole,
    ResearchBrief,
    ResearchDepth,
    ResearchModeAssessment,
    ResearchProfile,
    ResearchProgramComposition,
    ResearchStageResult,
)
from ..validation import compact_validation, validate_research_contract


class ResearchProgramError(RuntimeError):
    pass


class DualDirectorResearchWorkflow:
    """Independent Anchor/Expansion planning with claim-scoped correction loops."""

    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        anchor_director: AnchorDirectorAgent,
        expansion_director: ExpansionDirectorAgent,
        evaluator_a: ProgramEvaluatorAAgent,
        evaluator_b: ProgramEvaluatorBAgent,
        composer: ResearchProgramComposerAgent,
        deadline_policy: DeadlinePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.anchor_director = anchor_director
        self.expansion_director = expansion_director
        self.evaluator_a = evaluator_a
        self.evaluator_b = evaluator_b
        self.composer = composer
        self.deadline_policy = deadline_policy

    async def run(
        self,
        brief: ResearchBrief,
        assessment: ResearchModeAssessment,
        *,
        upstream_feedback: list[str] | None = None,
    ) -> ResearchStageResult:
        self.store.checkpoint("research-brief", brief)
        anchor_checkpoint = self._load_director_checkpoint(
            "anchor-director", brief, DirectorRole.ANCHOR
        )
        expansion_checkpoint = self._load_director_checkpoint(
            "expansion-director", brief, DirectorRole.EXPANSION
        )
        anchor = anchor_checkpoint
        expansion = expansion_checkpoint
        if upstream_feedback:
            # Upstream execution/review failures change the scientific contract.
            # Reopen both roles while still passing the exact feedback and prior
            # checkpoints as immutable history rather than silently reusing them.
            anchor = None
            expansion = None
        locked_ids: set[str] = set()
        anchor_notes: list[ClaimErrorNote] = []
        expansion_notes: list[ClaimErrorNote] = []
        last_evaluator_a: ClaimEvaluatorReport | None = None
        last_evaluator_b: ClaimEvaluatorReport | None = None

        for round_number in hypothesis_rounds(
            self.settings.max_hypothesis_rounds
        ):
            self._check_deadline(round_number)
            tasks = []
            roles = []
            if anchor is None or self._needs_role_revision(
                anchor,
                locked_ids,
                anchor_notes,
                required_promoted_count=1,
                required_claim_ids=(
                    {"A1"}
                    if brief.research_profile == ResearchProfile.TRACE_AUDIT
                    else set()
                ),
            ):
                roles.append(DirectorRole.ANCHOR)
                tasks.append(
                    self._generate_director(
                        role=DirectorRole.ANCHOR,
                        brief=brief,
                        assessment=assessment,
                        prior=anchor or anchor_checkpoint,
                        notes=anchor_notes,
                        locked_ids=locked_ids,
                        upstream_feedback=upstream_feedback or [],
                        round_number=round_number,
                    )
                )
            if expansion is None or self._needs_role_revision(
                expansion,
                locked_ids,
                expansion_notes,
                required_promoted_count=self._required_expansion_count(brief),
                required_claim_ids=set(),
            ):
                roles.append(DirectorRole.EXPANSION)
                tasks.append(
                    self._generate_director(
                        role=DirectorRole.EXPANSION,
                        brief=brief,
                        assessment=assessment,
                        prior=expansion or expansion_checkpoint,
                        notes=expansion_notes,
                        locked_ids=locked_ids,
                        upstream_feedback=upstream_feedback or [],
                        round_number=round_number,
                    )
                )
            if tasks:
                generated = await asyncio.gather(*tasks)
                for role, output in zip(roles, generated):
                    if role == DirectorRole.ANCHOR:
                        anchor = output
                    else:
                        expansion = output
            if anchor is None or expansion is None:
                raise ResearchProgramError("Both Director outputs are required")

            dependency_validation = validate_claim_dependencies(anchor, expansion)
            self.store.save(
                "claim-dependency-validation",
                compact_validation(dependency_validation),
                metadata={"round": round_number},
            )
            if not dependency_validation.valid:
                new_anchor_notes, new_expansion_notes = self._dependency_error_notes(
                    dependency_validation,
                    anchor,
                    expansion,
                    locked_ids,
                )
                anchor_notes = self._accumulate_error_notes(
                    anchor_notes, new_anchor_notes, locked_ids
                )
                expansion_notes = self._accumulate_error_notes(
                    expansion_notes, new_expansion_notes, locked_ids
                )
                continue

            evaluator_a, evaluator_a_id, evaluator_b, evaluator_b_id = (
                await self._evaluate_claims(
                    brief,
                    assessment,
                    anchor,
                    expansion,
                    round_number,
                    locked_ids,
                )
            )
            last_evaluator_a = evaluator_a
            last_evaluator_b = evaluator_b
            decision = compute_claim_promotions(evaluator_a, evaluator_b)
            all_claims = {
                item.claim_id: item for item in [*anchor.claims, *expansion.claims]
            }
            eligible_promoted = filter_promotions_by_dependencies(
                all_claims,
                set(decision.promoted_ids),
            )
            dependency_blocked = set(decision.promoted_ids) - eligible_promoted
            newly_locked = eligible_promoted - locked_ids
            locked_ids.update(eligible_promoted)
            if newly_locked:
                self.store.event(
                    "research_claims.locked",
                    {
                        "round": round_number,
                        "new_claim_ids": sorted(newly_locked),
                        "locked_claim_ids": sorted(locked_ids),
                    },
                )
            anchor_ids = {item.claim_id for item in anchor.claims}
            expansion_ids = {item.claim_id for item in expansion.claims}
            current_anchor_notes = self._merge_error_notes(
                evaluator_a,
                evaluator_b,
                role=DirectorRole.ANCHOR,
            )
            anchor_notes = self._accumulate_error_notes(
                anchor_notes, current_anchor_notes, locked_ids
            )
            current_expansion_notes = self._merge_error_notes(
                evaluator_a,
                evaluator_b,
                role=DirectorRole.EXPANSION,
            )
            expansion_notes = self._accumulate_error_notes(
                expansion_notes, current_expansion_notes, locked_ids
            )
            self.store.save(
                "claim-promotion-decision",
                {
                    "promoted_claim_ids": sorted(eligible_promoted),
                    "failed_claim_ids": sorted(
                        set(decision.failed_ids) | dependency_blocked
                    ),
                    "rejected_claim_ids": sorted(decision.rejected_ids),
                    "dependency_blocked_claim_ids": sorted(dependency_blocked),
                    "anchor_promoted": sorted(eligible_promoted & anchor_ids),
                    "expansion_promoted": sorted(
                        eligible_promoted & expansion_ids
                    ),
                },
                dependencies=[evaluator_a_id, evaluator_b_id],
                metadata={"round": round_number},
            )

            anchor_ready = (
                "A1" in eligible_promoted
                if brief.research_profile == ResearchProfile.TRACE_AUDIT
                else bool(eligible_promoted & anchor_ids)
            )
            expansion_ready = (
                len(eligible_promoted & expansion_ids)
                >= self._required_expansion_count(brief)
            )
            if (
                anchor_ready
                and self._required_expansion_count(brief)
                and not expansion_ready
                and not expansion_notes
            ):
                next_claim_id = self._next_claim_id("X", expansion_ids)
                depth_note = ClaimErrorNote(
                    claim_id=next_claim_id,
                    source_role=DirectorRole.EXPANSION,
                    failed_gates=["Research Depth Coverage"],
                    counterexample=(
                        f"{brief.research_depth.value} depth requires "
                        f"{self._required_expansion_count(brief)} independently "
                        "promoted Expansion claims."
                    ),
                    failure_cause=(
                        "The existing Expansion output has too few eligible claims "
                        "for the requested research depth."
                    ),
                    forbidden_revision=(
                        "Do not alter or relabel already locked claims merely to "
                        "increase the count."
                    ),
                    required_revision=(
                        f"Add a genuinely distinct, evidence-grounded {next_claim_id} "
                        "claim connected to the Anchor claim graph."
                    ),
                    preserve_claim_ids=sorted(locked_ids),
                )
                expansion_notes = self._accumulate_error_notes(
                    expansion_notes, [depth_note], locked_ids
                )
                self.store.save(
                    "claim-error-note",
                    depth_note,
                    metadata={
                        "round": round_number,
                        "evaluator": "deterministic-depth-gate",
                        "claim_id": next_claim_id,
                        "source_role": DirectorRole.EXPANSION,
                    },
                )
            if not anchor_ready or (
                self._required_expansion_count(brief) and not expansion_ready
            ):
                continue

            composition, composition_id = await self._compose(
                brief=brief,
                assessment=assessment,
                anchor=anchor,
                expansion=expansion,
                evaluator_a=evaluator_a,
                evaluator_b=evaluator_b,
                promoted_ids=eligible_promoted,
                failed_ids=(
                    set(decision.failed_ids | decision.rejected_ids)
                    | dependency_blocked
                ),
                round_number=round_number,
                dependencies=[evaluator_a_id, evaluator_b_id],
            )
            contract = build_research_contract_from_program(
                brief=brief,
                assessment=assessment,
                anchor=anchor,
                expansion=expansion,
                evaluator_a=evaluator_a,
                evaluator_b=evaluator_b,
                composition=composition,
                pipeline_smoke_test=self.settings.pipeline_smoke_test,
            )
            contract_validation = validate_research_contract(
                contract,
                expected_mode=contract.research_mode,
            )
            self.store.save(
                "research-program-contract-validation",
                compact_validation(contract_validation),
                dependencies=[composition_id],
                metadata={"round": round_number},
            )
            if not contract_validation.valid:
                raise ResearchProgramError(
                    "Composed Research Program produced an invalid Research Contract: "
                    f"{compact_validation(contract_validation)}"
                )
            trace_dependencies: list[str] = []
            if contract.trace_study_contract is not None:
                trace_id = self.store.save(
                    "trace-study-contract",
                    contract.trace_study_contract,
                    dependencies=[composition_id],
                    metadata={"frozen": True, "round": round_number},
                )
                trace_dependencies.append(trace_id)
            contract_id = self.store.save(
                "research-contract-final",
                contract,
                dependencies=[composition_id, *trace_dependencies],
                metadata={
                    "round": round_number,
                    "mode": contract.research_mode,
                    "source_stage": "DUAL_DIRECTOR_PROGRAM",
                },
            )
            result = ResearchStageResult(
                mode_assessment=assessment,
                contract=contract,
                source_stage="DUAL_DIRECTOR_PROGRAM",
                round_number=round_number,
                research_brief=brief,
                anchor_output=anchor,
                expansion_output=expansion,
                claim_evaluator_a=evaluator_a,
                claim_evaluator_b=evaluator_b,
                program_composition=composition,
            )
            self.store.checkpoint("research", result)
            self.store.checkpoint("research-program", result)
            self.store.event(
                "research_program.ready",
                {
                    "artifact_id": contract_id,
                    "round": round_number,
                    "integrated_claim_ids": composition.integrated_claim_ids,
                    "deferred_claim_ids": composition.deferred_claim_ids,
                },
            )
            return result

        missing = "Anchor"
        if last_evaluator_a is not None and last_evaluator_b is not None:
            decision = compute_claim_promotions(last_evaluator_a, last_evaluator_b)
            if decision.promoted_ids:
                missing = "required Expansion"
        raise ResearchProgramError(
            f"Dual-Director planning ended without a promoted {missing} claim"
        )

    async def _generate_director(
        self,
        *,
        role: DirectorRole,
        brief: ResearchBrief,
        assessment: ResearchModeAssessment,
        prior: ClaimDirectorOutput | None,
        notes: list[ClaimErrorNote],
        locked_ids: set[str],
        upstream_feedback: list[str],
        round_number: int,
    ) -> ClaimDirectorOutput:
        agent = (
            self.anchor_director
            if role == DirectorRole.ANCHOR
            else self.expansion_director
        )
        previous_id: str | None = None
        for attempt in repair_attempts(
            self.settings.max_component_repair_attempts
        ):
            payload = {
                "research_brief": brief.model_dump(mode="json"),
                "mode_assessment": assessment.model_dump(mode="json"),
                "round": round_number,
                "prior_output": prior.model_dump(mode="json") if prior else None,
                "individual_error_notebook": [
                    item.model_dump(mode="json") for item in notes
                ],
                "locked_claims": [
                    item.model_dump(mode="json")
                    for item in (prior.claims if prior else [])
                    if item.claim_id in locked_ids
                ],
                "upstream_feedback": upstream_feedback,
                "repair_attempt": attempt,
            }
            output = await agent.run(
                payload,
                session_label=(
                    f"{role.value.lower()}-director-round-{round_number}-attempt-{attempt}"
                ),
            )
            output = output.model_copy(
                update={
                    "director_role": role,
                    "research_objective": brief.research_objective,
                    "core_question": brief.core_question,
                }
            )
            if prior is not None and locked_ids:
                output = self._preserve_locked_claims(prior, output, locked_ids)
            output_id = self.store.save(
                f"{role.value.lower()}-director-output",
                output,
                dependencies=[previous_id] if previous_id else [],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            validation = validate_claim_director(
                output,
                brief=brief,
                role=role,
            )
            self.store.save(
                f"{role.value.lower()}-director-validation",
                compact_validation(validation),
                dependencies=[output_id],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            if validation.valid:
                stage = f"{role.value.lower()}-director"
                self.store.checkpoint(stage, output)
                return output
            previous_id = output_id
            notes = [
                ClaimErrorNote(
                    claim_id=(
                        output.claims[0].claim_id
                        if output.claims
                        else ("A1" if role == DirectorRole.ANCHOR else "X1")
                    ),
                    source_role=role,
                    failed_gates=[item.code for item in validation.issues],
                    counterexample="The deterministic Director validator rejected this output.",
                    failure_cause="; ".join(
                        item.message for item in validation.issues
                    ),
                    forbidden_revision="Do not alter locked claims or the user question.",
                    required_revision="Repair only the reported structural fields.",
                    preserve_claim_ids=sorted(locked_ids),
                )
            ]
        raise ResearchProgramError(f"{role.value} Director failed its repair budget")

    async def _evaluate_claims(
        self,
        brief: ResearchBrief,
        assessment: ResearchModeAssessment,
        anchor: ClaimDirectorOutput,
        expansion: ClaimDirectorOutput,
        round_number: int,
        locked_ids: set[str],
    ) -> tuple[
        ClaimEvaluatorReport,
        str,
        ClaimEvaluatorReport,
        str,
    ]:
        all_claim_ids = {item.claim_id for item in [*anchor.claims, *expansion.claims]}
        evidence_ids = {
            item.evidence_id for item in [*anchor.evidence, *expansion.evidence]
        }
        claim_roles = {
            item.claim_id: item.source_role
            for item in [*anchor.claims, *expansion.claims]
        }
        payload = {
            "research_brief": brief.model_dump(mode="json"),
            "mode_assessment": assessment.model_dump(mode="json"),
            "anchor_output": anchor.model_dump(mode="json"),
            "expansion_output": expansion.model_dump(mode="json"),
            "minimum_passing_score": self.settings.minimum_gate_score,
            "rubric_version": self.settings.rubric_version,
            "locked_claim_ids": sorted(locked_ids),
        }
        evaluator_a_gates = (
            TRACE_EVALUATOR_A_GATES
            if brief.research_profile == ResearchProfile.TRACE_AUDIT
            else EVALUATOR_A_GATES
        )
        payload["required_evaluator_a_gates"] = sorted(evaluator_a_gates)
        raw_a, raw_b = await asyncio.gather(
            self.evaluator_a.run(
                payload,
                session_label=f"program-evaluator-a-round-{round_number}",
            ),
            self.evaluator_b.run(
                payload,
                session_label=f"program-evaluator-b-round-{round_number}",
            ),
        )
        evaluator_a, evaluator_a_id = await self._repair_evaluator(
            role="a",
            agent=self.evaluator_a,
            output=raw_a,
            payload=payload,
            claim_ids=all_claim_ids,
            evidence_ids=evidence_ids,
            claim_roles=claim_roles,
            required_gates=evaluator_a_gates,
            round_number=round_number,
            locked_ids=locked_ids,
        )
        evaluator_b, evaluator_b_id = await self._repair_evaluator(
            role="b",
            agent=self.evaluator_b,
            output=raw_b,
            payload=payload,
            claim_ids=all_claim_ids,
            evidence_ids=evidence_ids,
            claim_roles=claim_roles,
            required_gates=EVALUATOR_B_GATES,
            round_number=round_number,
            locked_ids=locked_ids,
        )
        return evaluator_a, evaluator_a_id, evaluator_b, evaluator_b_id

    async def _repair_evaluator(
        self,
        *,
        role: str,
        agent: ProgramEvaluatorAAgent | ProgramEvaluatorBAgent,
        output: ClaimEvaluatorReport,
        payload: dict[str, Any],
        claim_ids: set[str],
        evidence_ids: set[str],
        claim_roles: dict[str, DirectorRole],
        required_gates: set[str],
        round_number: int,
        locked_ids: set[str],
    ) -> tuple[ClaimEvaluatorReport, str]:
        previous_id: str | None = None
        cumulative_validation_issues: list[dict[str, Any]] = []
        seen_validation_issues: set[tuple[str, str, str]] = set()
        for attempt in repair_attempts(
            self.settings.max_component_repair_attempts
        ):
            artifact_id = self.store.save(
                f"program-evaluator-{role}-report",
                output,
                dependencies=[previous_id] if previous_id else [],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            validation = validate_claim_evaluator(
                output,
                claim_ids=claim_ids,
                claim_roles=claim_roles,
                required_gates=required_gates,
                evidence_ids=evidence_ids,
                rubric_version=self.settings.rubric_version,
                minimum_passing_score=self.settings.minimum_gate_score,
                locked_claim_ids=locked_ids,
            )
            self.store.save(
                f"program-evaluator-{role}-validation",
                compact_validation(validation),
                dependencies=[artifact_id],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            if validation.valid:
                for note in output.error_notebook:
                    self.store.save(
                        "claim-error-note",
                        note,
                        dependencies=[artifact_id],
                        metadata={
                            "round": round_number,
                            "evaluator": role,
                            "claim_id": note.claim_id,
                            "source_role": note.source_role,
                        },
                    )
                return output, artifact_id
            current_validation_issues = compact_validation(validation)
            for issue in current_validation_issues:
                signature = (
                    str(issue["path"]),
                    str(issue["code"]),
                    str(issue["message"]),
                )
                if signature not in seen_validation_issues:
                    seen_validation_issues.add(signature)
                    cumulative_validation_issues.append(issue)
            if repair_budget_exhausted(
                attempt, self.settings.max_component_repair_attempts
            ):
                break
            previous_id = artifact_id
            output = await agent.run(
                {
                    **payload,
                    "repair_only": {
                        "prior_output": output.model_dump(mode="json"),
                        "validation_issues": current_validation_issues,
                        "cumulative_validation_issues": (
                            cumulative_validation_issues
                        ),
                        "instruction": (
                            "Repair only the invalid evaluator fields. Satisfy every "
                            "cumulative validation issue and do not regress fields "
                            "fixed in earlier attempts. Director outputs and "
                            "scientific inputs are frozen."
                        ),
                    },
                },
                session_label=(
                    f"program-evaluator-{role}-round-{round_number}-repair-{attempt + 1}"
                ),
            )
        raise ResearchProgramError(
            f"Program Evaluator {role.upper()} failed its repair budget"
        )

    async def _compose(
        self,
        *,
        brief: ResearchBrief,
        assessment: ResearchModeAssessment,
        anchor: ClaimDirectorOutput,
        expansion: ClaimDirectorOutput,
        evaluator_a: ClaimEvaluatorReport,
        evaluator_b: ClaimEvaluatorReport,
        promoted_ids: set[str],
        failed_ids: set[str],
        round_number: int,
        dependencies: list[str],
    ) -> tuple[ResearchProgramComposition, str]:
        payload = {
            "research_brief": brief.model_dump(mode="json"),
            "mode_assessment": assessment.model_dump(mode="json"),
            "anchor_output": anchor.model_dump(mode="json"),
            "expansion_output": expansion.model_dump(mode="json"),
            "evaluator_a": evaluator_a.model_dump(mode="json"),
            "evaluator_b": evaluator_b.model_dump(mode="json"),
            "deterministically_promoted_claim_ids": sorted(promoted_ids),
            "failed_or_rejected_claim_ids": sorted(failed_ids),
            "max_integrated_claims": self.settings.target_promoted_hypotheses,
        }
        output = await self.composer.run(
            payload,
            session_label=f"research-program-composer-round-{round_number}",
        )
        previous_id: str | None = None
        all_claim_ids = {item.claim_id for item in [*anchor.claims, *expansion.claims]}
        all_claims = {
            item.claim_id: item for item in [*anchor.claims, *expansion.claims]
        }
        anchor_ids = {item.claim_id for item in anchor.claims}
        expansion_ids = {item.claim_id for item in expansion.claims}
        for attempt in repair_attempts(
            self.settings.max_component_repair_attempts
        ):
            artifact_id = self.store.save(
                "research-program-composition",
                output,
                dependencies=dependencies + ([previous_id] if previous_id else []),
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            validation = validate_program_composition(
                output,
                all_claim_ids=all_claim_ids,
                promoted_claim_ids=promoted_ids,
                anchor_claim_ids=anchor_ids,
                expansion_claim_ids=expansion_ids,
                claim_dependencies={
                    claim_id: item.dependencies for claim_id, item in all_claims.items()
                },
                brief=brief,
                max_integrated_claims=self.settings.target_promoted_hypotheses,
                claim_tension_ids={
                    claim_id: item.tension_ids
                    for claim_id, item in all_claims.items()
                },
                available_tension_ids={
                    item.tension_id
                    for item in [*anchor.trace_tensions, *expansion.trace_tensions]
                },
            )
            self.store.save(
                "research-program-composition-validation",
                compact_validation(validation),
                dependencies=[artifact_id],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            if validation.valid:
                return output, artifact_id
            if repair_budget_exhausted(
                attempt, self.settings.max_component_repair_attempts
            ):
                break
            previous_id = artifact_id
            output = await self.composer.run(
                {
                    **payload,
                    "repair_only": {
                        "prior_output": output.model_dump(mode="json"),
                        "validation_issues": compact_validation(validation),
                        "instruction": (
                            "Repair only composition, stage ordering, and claim ID "
                            "selection. Promotion decisions are frozen."
                        ),
                    },
                },
                session_label=(
                    f"research-program-composer-round-{round_number}-repair-{attempt + 1}"
                ),
            )
        raise ResearchProgramError("Research Program Composer failed its repair budget")

    def _load_director_checkpoint(
        self,
        stage: str,
        brief: ResearchBrief,
        role: DirectorRole,
    ) -> ClaimDirectorOutput | None:
        value = self.store.load_checkpoint(stage, ClaimDirectorOutput)
        if value is None:
            return None
        if (
            value.core_question != brief.core_question
            or value.research_objective != brief.research_objective
            or value.director_role != role
        ):
            return None
        return value

    @staticmethod
    def _merge_error_notes(
        evaluator_a: ClaimEvaluatorReport,
        evaluator_b: ClaimEvaluatorReport,
        *,
        role: DirectorRole,
    ) -> list[ClaimErrorNote]:
        merged: dict[str, ClaimErrorNote] = {}
        for note in [*evaluator_a.error_notebook, *evaluator_b.error_notebook]:
            if note.source_role != role:
                continue
            prior = merged.get(note.claim_id)
            if prior is None:
                merged[note.claim_id] = note
                continue
            merged[note.claim_id] = prior.model_copy(
                update={
                    "failed_gates": list(
                        dict.fromkeys([*prior.failed_gates, *note.failed_gates])
                    ),
                    "counterexample": prior.counterexample + " | " + note.counterexample,
                    "failure_cause": prior.failure_cause + " | " + note.failure_cause,
                    "required_revision": (
                        prior.required_revision + " | " + note.required_revision
                    ),
                    "preserve_claim_ids": list(
                        dict.fromkeys(
                            [*prior.preserve_claim_ids, *note.preserve_claim_ids]
                        )
                    ),
                }
            )
        return list(merged.values())

    @staticmethod
    def _accumulate_error_notes(
        existing: list[ClaimErrorNote],
        current: list[ClaimErrorNote],
        locked_ids: set[str],
    ) -> list[ClaimErrorNote]:
        """Keep one cumulative, compressed notebook entry per unresolved claim."""

        merged: dict[str, ClaimErrorNote] = {}
        for note in [*existing, *current]:
            if note.claim_id in locked_ids:
                continue
            prior = merged.get(note.claim_id)
            if prior is None:
                merged[note.claim_id] = note
                continue
            merged[note.claim_id] = prior.model_copy(
                update={
                    "failed_gates": list(
                        dict.fromkeys([*prior.failed_gates, *note.failed_gates])
                    ),
                    "counterexample": prior.counterexample + " | " + note.counterexample,
                    "failure_cause": prior.failure_cause + " | " + note.failure_cause,
                    "required_revision": (
                        prior.required_revision + " | " + note.required_revision
                    ),
                    "preserve_claim_ids": list(
                        dict.fromkeys(
                            [*prior.preserve_claim_ids, *note.preserve_claim_ids]
                        )
                    ),
                }
            )
        return list(merged.values())

    @staticmethod
    def _dependency_error_notes(
        validation: Any,
        anchor: ClaimDirectorOutput,
        expansion: ClaimDirectorOutput,
        locked_ids: set[str],
    ) -> tuple[list[ClaimErrorNote], list[ClaimErrorNote]]:
        claims = {item.claim_id: item for item in [*anchor.claims, *expansion.claims]}
        affected_ids = {
            claim_id
            for claim_id in claims
            if any(
                f"/claims/{claim_id}/" in issue.path
                for issue in validation.issues
            )
        }
        if any(issue.code == "CYCLIC_CLAIM_DEPENDENCY" for issue in validation.issues):
            affected_ids.update(
                claim_id for claim_id, claim in claims.items() if claim.dependencies
            )
        if not affected_ids:
            affected_ids.update(claims)
        issues_by_claim = {
            claim_id: [
                issue
                for issue in validation.issues
                if f"/claims/{claim_id}/" in issue.path
                or issue.code == "CYCLIC_CLAIM_DEPENDENCY"
            ]
            for claim_id in affected_ids
        }
        notes: dict[DirectorRole, list[ClaimErrorNote]] = {
            DirectorRole.ANCHOR: [],
            DirectorRole.EXPANSION: [],
        }
        for claim_id in sorted(affected_ids - locked_ids):
            claim = claims[claim_id]
            issues = issues_by_claim[claim_id] or validation.issues
            notes[claim.source_role].append(
                ClaimErrorNote(
                    claim_id=claim_id,
                    source_role=claim.source_role,
                    failed_gates=[item.code for item in issues],
                    counterexample=(
                        "The current claim graph cannot establish a valid staged "
                        "relationship between this claim and the research anchor."
                    ),
                    failure_cause="; ".join(item.message for item in issues),
                    forbidden_revision="Do not rename unrelated or locked claims.",
                    required_revision="Repair only the reported dependency edges.",
                    preserve_claim_ids=sorted(locked_ids),
                )
            )
        return notes[DirectorRole.ANCHOR], notes[DirectorRole.EXPANSION]

    @staticmethod
    def _preserve_locked_claims(
        prior: ClaimDirectorOutput,
        revised: ClaimDirectorOutput,
        locked_ids: set[str],
    ) -> ClaimDirectorOutput:
        prior_claims = {item.claim_id: item for item in prior.claims}
        revised_claims = {item.claim_id: item for item in revised.claims}
        for claim_id in locked_ids:
            if claim_id in prior_claims:
                revised_claims[claim_id] = prior_claims[claim_id]
        ordered_ids = [item.claim_id for item in revised.claims]
        for claim_id in prior_claims:
            if claim_id in locked_ids and claim_id not in ordered_ids:
                ordered_ids.append(claim_id)
        evidence = {item.evidence_id: item for item in revised.evidence}
        for item in prior.evidence:
            evidence.setdefault(item.evidence_id, item)
        return revised.model_copy(
            update={
                "claims": [revised_claims[claim_id] for claim_id in ordered_ids],
                "evidence": list(evidence.values()),
            }
        )

    @staticmethod
    def _needs_role_revision(
        output: ClaimDirectorOutput,
        locked_ids: set[str],
        notes: list[ClaimErrorNote],
        *,
        required_promoted_count: int,
        required_claim_ids: set[str],
    ) -> bool:
        if required_promoted_count == 0:
            return False
        if not notes:
            return False
        if required_claim_ids and not required_claim_ids.issubset(locked_ids):
            return True
        output_ids = {item.claim_id for item in output.claims}
        if len(output_ids.intersection(locked_ids)) >= required_promoted_count:
            return False
        return True

    @staticmethod
    def _next_claim_id(prefix: str, existing_ids: set[str]) -> str:
        index = 1
        while f"{prefix}{index}" in existing_ids:
            index += 1
        return f"{prefix}{index}"

    @staticmethod
    def _required_expansion_count(brief: ResearchBrief) -> int:
        return {
            ResearchDepth.QUICK: 0,
            ResearchDepth.COMPETITION: 1,
            ResearchDepth.THESIS: 1,
            ResearchDepth.PUBLICATION: 2,
        }[brief.research_depth]

    def _check_deadline(self, round_number: int) -> None:
        if self.deadline_policy is None:
            return
        decision = self.deadline_policy.can_start(RuntimePhase.RESEARCH_PLANNING)
        if not decision.allowed:
            raise ResearchProgramError(
                f"Research Program round {round_number} blocked: {decision.reason}"
            )
