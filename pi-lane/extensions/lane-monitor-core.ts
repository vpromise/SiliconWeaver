export type NoticeType = "info" | "warning" | "error";

export interface Snapshot {
	kind: "workflow" | "rtl_closure" | "lane" | "pdk" | "image";
	runId: string;
	status: string;
	phase: string;
	currentStep?: string;
	lastCompletedStep?: string;
	completedSteps: number;
	elapsedSeconds: number;
	errors: string[];
	laneRunId?: string;
	rtlClosureRunId?: string;
	workflowRevision?: number;
	failureCode?: string;
	failureDomain?: string;
	failureMessage?: string;
	failureRetryable?: boolean;
	failureRepairableByRtl?: boolean;
	failureUserActionRequired?: boolean;
	failureEvidence?: string[];
	visualizationStatus?: string;
	visualizationCount?: number;
	latestVisualization?: string;
	visualizationErrors?: string[];
	stepHistory?: StepRecord[];
	qorGate?: string;
	signoffGate?: string;
	physicalSanityGate?: string;
	qorReasons?: string[];
	processStatus?: string;
	flowScope?: string;
	deliveryGate?: string;
	deliveryReasons?: string[];
	expectedFinalStep?: string;
	endpointReached?: boolean;
	mappedEquivalenceStatus?: string;
	mappedEquivalenceGate?: string;
	mappedEquivalenceScope?: string;
	mappedEquivalenceReasons?: string[];
	openPdk?: boolean;
	foundrySignoff?: boolean;
	tapeoutGuarantee?: boolean;
	assuranceLimitations?: string[];
	repairRound?: number;
	repairMaxRounds?: number;
	repairCandidate?: number;
	repairCandidateCount?: number;
	repairFailureClass?: string;
	repairRequirementId?: string;
	repairFirstFailureCycle?: number;
	repairStrategy?: string;
	repairCandidateResult?: string;
	repairCandidateEligible?: boolean;
	repairCandidateReason?: string;
	repairCandidateScore?: number | number[];
	repairStagnation?: number;
	repairAction?: string;
	adaptiveAddedScenarios?: number;
	adaptiveEpoch?: number;
	suiteSha256?: string;
	formalMode?: string;
	formalStatus?: string;
	formalDetail?: string;
	formalArtifact?: string;
	universalMode?: string;
	universalStatus?: string;
	universalDetail?: string;
	universalArtifact?: string;
	universalPropertyCount?: number;
	universalPassedCount?: number;
	universalFailedPropertyIds?: string[];
	universalProof?: boolean;
	verilatorStatus?: string;
	counterexampleCount?: number;
	latestAttempt?: string;
	verificationPassed?: boolean;
	verificationChecks?: number;
	verificationMismatches?: number;
	coverageRound?: number;
	coverageMaxRounds?: number;
	coveragePercent?: number;
	coverageTargetPercent?: number;
	coverageRemainingCycles?: number;
	coverageStatus?: string;
	coverageTargetMet?: boolean;
	coverageWarning?: string;
	coverageError?: string;
	architectureAuditStatus?: string;
	architectureFailedChecks?: string[];
	architectureAuditArtifact?: string;
	repairArchitectureFailedChecks?: string[];
	repairArchitectureArtifact?: string;
	synthesisEquivalenceStatus?: string;
	synthesisEquivalenceCells?: number;
	synthesisEquivalenceProvenCells?: number;
	synthesisEquivalenceArtifact?: string;
	synthesisEquivalenceLog?: string;
	modelRequest?: ModelRequestProgress;
	events?: RtlClosureEvent[];
}

export type ModelRequestEvent =
	| "request_started"
	| "request_waiting"
	| "stream_connected"
	| "streaming"
	| "request_completed"
	| "request_retrying"
	| "request_failed";

export interface ModelRequestProgress {
	event: ModelRequestEvent;
	requestId?: number;
	candidate?: number;
	candidateCount?: number;
	attempt?: number;
	maxAttempts?: number;
	activeRequestCount?: number;
	elapsedSeconds?: number;
	firstEventSeconds?: number;
	chunkCount?: number;
	retryInSeconds?: number;
	retryAfterSeconds?: number;
	httpStatus?: number;
	retryable?: boolean;
	errorType?: string;
	rayId?: string;
}

export interface RtlClosureEvent {
	sequence: number;
	timestamp?: string;
	status: string;
	phase: string;
	repair?: Record<string, unknown>;
	coverage?: Record<string, unknown>;
	verification?: Record<string, unknown>;
	architectureAudit?: Record<string, unknown>;
	synthesisEquivalence?: Record<string, unknown>;
	universalMode?: string;
	universalStatus?: string;
	universalDetail?: string;
	universalArtifact?: string;
	universalPropertyCount?: number;
	universalPassedCount?: number;
	universalFailedPropertyIds?: string[];
	universalProof?: boolean;
	modelRequest?: ModelRequestProgress;
	error?: string;
}

export interface EventGap {
	expected: number;
	actual: number;
}

export interface EventDrain {
	cursor: number;
	pending: RtlClosureEvent[];
	gaps: EventGap[];
	initialAttach: boolean;
}

export interface MonitorHealth {
	degraded: boolean;
	consecutiveFailures: number;
	error?: string;
	transition: "degraded" | "recovered" | "unchanged";
}

export type MonitorSource = "workflow" | "rtl_closure" | "lane" | "pdk" | "image";

export interface OrchestratorEnvelopeResult {
	value?: Record<string, unknown>;
	errorCode?: string;
	errorMessage?: string;
	errorCategory?: string;
	retryable?: boolean;
}

export interface OrchestratorListEnvelopeResult {
	values?: Record<string, unknown>[];
	errorCode?: string;
	errorMessage?: string;
	errorCategory?: string;
	retryable?: boolean;
}

export interface WorkflowEvent {
	sequence: number;
	eventId: string;
	timestamp: string;
	source: string;
	attemptId?: string;
	type: string;
	severity: NoticeType;
	stateRevision: number;
	phase: string;
	payload: Record<string, unknown>;
	failure?: Record<string, unknown>;
	previousSha256?: string;
	sha256: string;
}

export interface WorkflowEventCursor {
	afterSequence: number;
	cursorSha256?: string;
	suppressHistory: boolean;
}

export interface WorkflowEventReport {
	workflowId: string;
	events: WorkflowEvent[];
	snapshot: Snapshot;
	nextCursor: WorkflowEventCursor;
	hasMore: boolean;
	acknowledgedSequence?: number;
}

export interface LaneTimelineEvent {
	sequence: number;
	event: string;
	timestamp?: string;
	phase?: string;
	status?: string;
	severity?: NoticeType;
	message?: string;
	ordinal?: number;
	step?: string;
	directory?: string;
	commands?: string;
	logs: string[];
	image?: string;
	error?: string;
	attempt?: number;
}

export interface LaneEventCursor {
	afterSequence: number;
	generation?: string;
	cursorSha256: string;
	suppressHistory: boolean;
}

export interface LaneEventReport {
	runId: string;
	events: LaneTimelineEvent[];
	snapshot: Snapshot;
	nextCursor: LaneEventCursor;
	hasMore: boolean;
	invalidLineCount: number;
}

export interface MonitorCheckpoint {
	schemaVersion: 6;
	clientId?: string;
	workflowAutoDiscovery: boolean;
	workflowClaim?: string;
	recentTerminalWorkflow?: string;
	bindings: Partial<Record<MonitorSource, string>>;
	pinnedSources: MonitorSource[];
	workflowEventCursors: Record<string, WorkflowEventCursor>;
	rtlClosureEventCursors: Record<string, number>;
	laneEventCursors: Record<string, LaneEventCursor>;
}

export interface StepRecord {
	ordinal: number;
	id: string;
	phase: string;
	status: string;
	directory: string;
	commands?: string;
	logs: string[];
	runtime?: string;
	visualizationStatus: string;
	visualization?: string;
	visualizationError?: string;
}

interface RepairDetails {
	round?: number;
	maxRounds?: number;
	candidate?: number;
	candidateCount?: number;
	failureClass?: string;
	requirementId?: string;
	firstFailureCycle?: number;
	strategy?: string;
	candidateResult?: string;
	candidateEligible?: boolean;
	candidateReason?: string;
	candidateScore?: number | number[];
	stagnation?: number;
	action?: string;
	adaptiveAddedScenarios?: number;
	adaptiveEpoch?: number;
	suiteSha256?: string;
	formalMode?: string;
	formalStatus?: string;
	formalDetail?: string;
	formalArtifact?: string;
	universalMode?: string;
	universalStatus?: string;
	universalDetail?: string;
	universalArtifact?: string;
	universalPropertyCount?: number;
	universalPassedCount?: number;
	universalFailedPropertyIds?: string[];
	universalProof?: boolean;
	verilatorStatus?: string;
	counterexampleCount?: number;
	latestAttempt?: string;
	architectureFailedChecks?: string[];
	architectureArtifact?: string;
}

const FAILURE_CLEAR_PHASES = new Set([
	"regression",
	"independent_verification",
	"rtl_validated",
	"publishing",
	"published",
	"lane_starting",
	"lane_running",
	"completed",
]);

const MODEL_REQUEST_EVENTS = new Set<ModelRequestEvent>([
	"request_started",
	"request_waiting",
	"stream_connected",
	"streaming",
	"request_completed",
	"request_retrying",
	"request_failed",
]);

export function record(value: unknown): Record<string, unknown> | undefined {
	if (value === null || typeof value !== "object" || Array.isArray(value)) {
		return undefined;
	}
	return value as Record<string, unknown>;
}

/** Parse the current direct-object response used by Lane and RTL-Closure CLIs. */
export function jsonObjectPayload(value: unknown): Record<string, unknown> | undefined {
	const outer = record(value);
	if (!outer) return undefined;
	if (
		outer.ok !== undefined
		|| outer.data !== undefined
		|| outer.result !== undefined
	) return undefined;
	return outer;
}

/** Parse the current direct object-array response used by Lane and RTL-Closure. */
export function jsonObjectArrayPayload(value: unknown): Record<string, unknown>[] | undefined {
	if (!Array.isArray(value)) return undefined;
	const values: Record<string, unknown>[] = [];
	for (const item of value) {
		const parsed = record(item);
		if (!parsed) return undefined;
		values.push(parsed);
	}
	return values;
}

const ORCHESTRATOR_READ_PROTOCOLS = new Set([3]);

export function orchestratorEnvelope(
	value: unknown,
	expectedKind: "run.get" | "run.events",
): OrchestratorEnvelopeResult | undefined {
	const outer = record(value);
	if (
		!outer
		|| finiteNumber(outer.schema_version) !== 1
		|| !ORCHESTRATOR_READ_PROTOCOLS.has(finiteNumber(outer.protocol_version) ?? -1)
		|| booleanValue(outer.ok) === undefined
		|| stringValue(outer.kind) !== expectedKind && stringValue(outer.kind) !== "error"
		|| (
			outer.request_id !== null
			&& outer.request_id !== undefined
			&& stringValue(outer.request_id) === undefined
		)
	) {
		return undefined;
	}
	if (outer.ok === true) {
		if (outer.kind !== expectedKind) return undefined;
		const data = record(outer.data);
		return data ? { value: data } : undefined;
	}
	if (outer.kind !== "error") return undefined;
	const error = record(outer.error);
	const errorCode = boundedIdentifier(error?.code);
	const errorMessage = stringValue(error?.message);
	const errorCategory = boundedIdentifier(error?.category);
	const retryable = booleanValue(error?.retryable);
	if (!errorCode || !errorMessage || !errorCategory || retryable === undefined) {
		return undefined;
	}
	return { errorCode, errorMessage, errorCategory, retryable };
}

export function orchestratorListEnvelope(value: unknown): OrchestratorListEnvelopeResult | undefined {
	const outer = record(value);
	if (
		!outer
		|| finiteNumber(outer.schema_version) !== 1
		|| !ORCHESTRATOR_READ_PROTOCOLS.has(finiteNumber(outer.protocol_version) ?? -1)
		|| booleanValue(outer.ok) === undefined
		|| stringValue(outer.kind) !== "run.list" && stringValue(outer.kind) !== "error"
		|| (
			outer.request_id !== null
			&& outer.request_id !== undefined
			&& stringValue(outer.request_id) === undefined
		)
	) {
		return undefined;
	}
	if (outer.ok === true) {
		if (outer.kind !== "run.list") return undefined;
		const values = jsonObjectArrayPayload(outer.data);
		return values ? { values } : undefined;
	}
	if (outer.kind !== "error") return undefined;
	const error = record(outer.error);
	const errorCode = boundedIdentifier(error?.code);
	const errorMessage = stringValue(error?.message);
	const errorCategory = boundedIdentifier(error?.category);
	const retryable = booleanValue(error?.retryable);
	if (!errorCode || !errorMessage || !errorCategory || retryable === undefined) {
		return undefined;
	}
	return { errorCode, errorMessage, errorCategory, retryable };
}

export function finiteNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function booleanValue(value: unknown): boolean | undefined {
	return typeof value === "boolean" ? value : undefined;
}

export function stringValue(value: unknown): string | undefined {
	return typeof value === "string" && value.length > 0 ? value : undefined;
}

function firstString(...values: unknown[]): string | undefined {
	for (const value of values) {
		const parsed = stringValue(value);
		if (parsed !== undefined) return parsed;
	}
	return undefined;
}

function firstNumber(...values: unknown[]): number | undefined {
	for (const value of values) {
		const parsed = finiteNumber(value);
		if (parsed !== undefined) return parsed;
	}
	return undefined;
}

function firstBoolean(...values: unknown[]): boolean | undefined {
	for (const value of values) {
		const parsed = booleanValue(value);
		if (parsed !== undefined) return parsed;
	}
	return undefined;
}

function optionalStrings(value: unknown): string[] | undefined {
	return value === undefined ? undefined : strings(value);
}

function stringArrayCount(value: unknown): number | undefined {
	return Array.isArray(value) ? strings(value).length : undefined;
}

function scoreValue(value: unknown): number | number[] | undefined {
	const scalar = finiteNumber(value);
	if (scalar !== undefined) return scalar;
	if (!Array.isArray(value) || value.length === 0) return undefined;
	const values = value.map(finiteNumber);
	if (values.some((item) => item === undefined)) return undefined;
	return values as number[];
}

function nonNegativeNumber(...values: unknown[]): number | undefined {
	for (const value of values) {
		const parsed = finiteNumber(value);
		if (parsed !== undefined && parsed >= 0) return parsed;
	}
	return undefined;
}

function boundedIdentifier(value: unknown): string | undefined {
	const parsed = stringValue(value);
	if (!parsed || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(parsed)) {
		return undefined;
	}
	return parsed;
}

function modelRequestProgress(value: unknown): ModelRequestProgress | undefined {
	const raw = record(value);
	if (!raw) return undefined;
	const eventValue = stringValue(raw.event);
	if (!eventValue || !MODEL_REQUEST_EVENTS.has(eventValue as ModelRequestEvent)) {
		return undefined;
	}

	const requestId = nonNegativeNumber(raw.request_id);
	const activeRequests = Array.isArray(raw.active_requests)
		? raw.active_requests.flatMap((item) => {
			const parsed = record(item);
			return parsed ? [parsed] : [];
		})
		: [];
	const active = activeRequests.find(
		(item) => requestId !== undefined && finiteNumber(item.request_id) === requestId,
	) ?? (activeRequests.length === 1 ? activeRequests[0] : {});

	return {
		event: eventValue as ModelRequestEvent,
		requestId,
		candidate: nonNegativeNumber(raw.candidate_index, active.candidate_index),
		candidateCount: nonNegativeNumber(raw.candidate_count, active.candidate_count),
		attempt: nonNegativeNumber(raw.attempt, active.attempt),
		maxAttempts: nonNegativeNumber(raw.max_attempts, active.max_attempts),
		activeRequestCount: nonNegativeNumber(raw.active_request_count),
		elapsedSeconds: nonNegativeNumber(raw.elapsed_seconds, active.elapsed_seconds),
		firstEventSeconds: nonNegativeNumber(
			raw.first_event_seconds,
			active.first_event_seconds,
		),
		chunkCount: nonNegativeNumber(raw.chunk_count, active.chunk_count),
		retryInSeconds: nonNegativeNumber(raw.retry_in_seconds),
		retryAfterSeconds: nonNegativeNumber(raw.retry_after_seconds),
		httpStatus: nonNegativeNumber(raw.http_status),
		retryable: booleanValue(raw.retryable),
		errorType: boundedIdentifier(raw.error_type),
		rayId: boundedIdentifier(raw.ray_id),
	};
}

export function strings(value: unknown): string[] {
	const scalar = stringValue(value);
	if (scalar !== undefined) return [scalar];
	if (!Array.isArray(value)) return [];
	return value.flatMap((item) => {
		const parsed = stringValue(item);
		return parsed === undefined ? [] : [parsed];
	});
}

export function monitorRunId(value: unknown): string | undefined {
	const parsed = stringValue(value);
	if (!parsed || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$/.test(parsed)) return undefined;
	return parsed;
}

export function sourceRunKey(source: MonitorSource, runId: string): string {
	return `${source}:${runId}`;
}

export function compactNotice(value: string, maximumCharacters = 8_192): string {
	const limit = Math.max(256, Math.min(32_768, maximumCharacters));
	const lines = value.split("\n");
	const selected = lines.length > 24
		? [...lines.slice(0, 20), `... ${lines.length - 24} line(s) omitted ...`, ...lines.slice(-4)]
		: lines;
	const text = selected.join("\n");
	if (text.length <= limit) return text;
	return `${text.slice(0, Math.max(0, limit - 20)).trimEnd()}\n... truncated ...`;
}

export function isManagedStateAbsent(value: string): boolean {
	return (
		/\bno (?:(?:rtl[-_ ]closure|managed) )?(?:librelane )?(?:run|(?:image|pdk) installation) exists\b/i
		.test(value)
		|| /\b(?:run|(?:image|pdk) installation) does not exist(?::|\b)/i.test(value)
	);
}

export function nextMonitorHealth(
	previous: MonitorHealth,
	errors: string[],
): MonitorHealth {
	if (errors.length > 0) {
		return {
			degraded: true,
			consecutiveFailures: previous.consecutiveFailures + 1,
			error: errors.join("; ").slice(0, 1200),
			transition: previous.degraded ? "unchanged" : "degraded",
		};
	}
	return {
		degraded: false,
		consecutiveFailures: 0,
		transition: previous.degraded ? "recovered" : "unchanged",
	};
}

export function heartbeatDue(
	status: string,
	lastHeartbeatAt: number | undefined,
	now: number,
	interval: number,
): boolean {
	return status === "running"
		&& lastHeartbeatAt !== undefined
		&& interval > 0
		&& now - lastHeartbeatAt >= interval;
}

function candidateResult(value: unknown): string | undefined {
	const scalar = stringValue(value);
	if (scalar !== undefined) return scalar;
	const result = record(value);
	if (!result) return undefined;
	const passed = booleanValue(result.passed);
	if (passed === undefined) return undefined;
	if (passed) return "passed";
	const failureClass = stringValue(result.failure_class);
	return failureClass ? `failed (${failureClass})` : "failed";
}

function clearsFailure(
	status: string,
	phase: string,
	repair: Record<string, unknown>,
	verification: Record<string, unknown>,
): boolean {
	return status === "completed"
		|| FAILURE_CLEAR_PHASES.has(phase)
		|| booleanValue(repair.passed) === true
		|| (
			phase === "formal_completed"
			&& stringValue(repair.formal_status) === "pass"
		)
		|| booleanValue(verification.passed) === true;
}

function repairDetails(
	status: string,
	phase: string,
	repair: Record<string, unknown>,
	verification: Record<string, unknown>,
): RepairDetails {
	const clear = clearsFailure(status, phase, repair, verification);
	const formal = record(verification.formal) ?? {};
	const universal = record(verification.universal_properties) ?? {};
	return {
		round: finiteNumber(repair.round),
		maxRounds: finiteNumber(repair.max_rounds),
		candidate: finiteNumber(repair.candidate),
		candidateCount: finiteNumber(repair.candidate_count),
		failureClass: clear ? undefined : stringValue(repair.failure_class),
		requirementId: clear ? undefined : stringValue(repair.requirement_id),
		firstFailureCycle: clear ? undefined : finiteNumber(repair.first_failure_cycle),
		strategy: stringValue(repair.strategy),
		candidateResult: candidateResult(
			repair.candidate_result ?? repair.result,
		),
		candidateEligible: booleanValue(
			repair.candidate_eligible ?? repair.eligible,
		),
		candidateReason: firstString(repair.candidate_reason, repair.reason),
		candidateScore: scoreValue(repair.candidate_score ?? repair.score),
		stagnation: finiteNumber(repair.stagnation),
		action: stringValue(repair.action),
		adaptiveAddedScenarios: finiteNumber(repair.added_scenario_count),
		adaptiveEpoch: finiteNumber(repair.epoch),
		suiteSha256: firstString(repair.suite_sha256, repair.parent_suite_sha256),
		formalMode: firstString(
			verification.formal_mode,
			formal.mode,
			repair.formal_mode,
		),
		formalStatus: firstString(formal.status, repair.formal_status),
		formalDetail: firstString(formal.detail, repair.formal_detail),
		formalArtifact: firstString(formal.artifact, repair.formal_artifact),
		universalMode: firstString(
			verification.universal_mode,
			universal.mode,
			repair.universal_mode,
		),
		universalStatus: firstString(universal.status, repair.universal_status),
		universalDetail: firstString(universal.detail, repair.universal_detail),
		universalArtifact: firstString(
			universal.artifact,
			repair.universal_artifact,
		),
		universalPropertyCount: firstNumber(
			universal.property_count,
			repair.property_count,
		),
		universalPassedCount: firstNumber(
			universal.passed_count,
			universal.passed_property_count,
			stringArrayCount(universal.passed_property_ids),
			repair.passed_count,
			repair.passed_property_count,
		),
		universalFailedPropertyIds: optionalStrings(
			universal.failed_property_ids ?? repair.failed_property_ids,
		),
		universalProof: firstBoolean(
			universal.universal_proof,
			repair.universal_proof,
		),
		verilatorStatus: firstString(
			record(formal.verilator_lint)?.status,
			record(verification.verilator_lint)?.status,
			repair.verilator_status,
		),
		counterexampleCount: finiteNumber(repair.counterexample_count),
		latestAttempt: stringValue(repair.latest_attempt),
		architectureFailedChecks: optionalStrings(repair.failed_checks),
		architectureArtifact: stringValue(repair.architecture_artifact),
	};
}

export function rtlClosureEvents(value: unknown): RtlClosureEvent[] {
	if (!Array.isArray(value)) return [];
	const parsed = value.flatMap((item) => {
		const raw = record(item);
		if (!raw) return [];
		const sequence = finiteNumber(raw.sequence);
		const status = stringValue(raw.status);
		const phase = stringValue(raw.phase);
		if (
			sequence === undefined
			|| !Number.isInteger(sequence)
			|| sequence < 1
			|| status === undefined
			|| phase === undefined
		) {
			return [];
		}
		const repair = record(raw.repair);
		const verification = record(raw.verification);
		const details = repairDetails(
			status,
			phase,
			repair ?? {},
			verification ?? {},
		);
		return [{
			sequence,
			timestamp: stringValue(raw.timestamp),
			status,
			phase,
			repair,
			coverage: record(raw.coverage),
			verification,
			architectureAudit: record(raw.architecture_audit),
			synthesisEquivalence: record(raw.synthesis_equivalence),
			universalMode: details.universalMode,
			universalStatus: details.universalStatus,
			universalDetail: details.universalDetail,
			universalArtifact: details.universalArtifact,
			universalPropertyCount: details.universalPropertyCount,
			universalPassedCount: details.universalPassedCount,
			universalFailedPropertyIds: details.universalFailedPropertyIds,
			universalProof: details.universalProof,
			modelRequest: modelRequestProgress(raw.model_request),
			error: stringValue(raw.error),
		}];
	});
	parsed.sort((left, right) => left.sequence - right.sequence);
	const unique = new Map<number, RtlClosureEvent>();
	for (const event of parsed) unique.set(event.sequence, event);
	return [...unique.values()];
}

export function drainRtlClosureEvents(
	events: RtlClosureEvent[],
	previous: number | undefined,
): EventDrain {
	const newest = events.at(-1)?.sequence ?? previous ?? 0;
	if (previous === undefined) {
		return { cursor: newest, pending: [], gaps: [], initialAttach: true };
	}
	const pending = events.filter((event) => event.sequence > previous);
	const gaps: EventGap[] = [];
	let expected = previous + 1;
	for (const event of pending) {
		if (event.sequence > expected) gaps.push({ expected, actual: event.sequence });
		expected = event.sequence + 1;
	}
	return {
		cursor: Math.max(previous, newest),
		pending,
		gaps,
		initialAttach: false,
	};
}

function scoreText(value: number | number[]): string {
	return Array.isArray(value) ? `[${value.join(", ")}]` : String(value);
}

function appendRepairLines(lines: string[], details: RepairDetails): void {
	if (details.round !== undefined) {
		lines.push(
			`repair: round ${details.round}/${details.maxRounds ?? "?"}`
			+ `, candidate ${details.candidate ?? 0}/${details.candidateCount ?? 0}`,
		);
	}
	if (details.requirementId) lines.push(`requirement: ${details.requirementId}`);
	if (details.failureClass) lines.push(`failure class: ${details.failureClass}`);
	if (details.firstFailureCycle !== undefined) {
		lines.push(`first failure cycle: ${details.firstFailureCycle}`);
	}
	if (details.strategy) lines.push(`strategy: ${details.strategy}`);
	if (details.candidateResult) lines.push(`candidate result: ${details.candidateResult}`);
	if (details.candidateEligible !== undefined) {
		lines.push(`candidate eligible: ${details.candidateEligible}`);
	}
	if (details.candidateReason) lines.push(`candidate reason: ${details.candidateReason}`);
	if (details.candidateScore !== undefined) {
		lines.push(`candidate score: ${scoreText(details.candidateScore)}`);
	}
	if (details.stagnation !== undefined) lines.push(`stagnation: ${details.stagnation}`);
	if (details.action) lines.push(`action: ${details.action}`);
	if (details.adaptiveAddedScenarios !== undefined) {
		lines.push(`adaptive scenarios: +${details.adaptiveAddedScenarios}`);
	}
	if (details.adaptiveEpoch !== undefined) lines.push(`suite epoch: ${details.adaptiveEpoch}`);
	if (details.suiteSha256) lines.push(`suite: ${details.suiteSha256}`);
	if (details.formalMode) lines.push(`formal mode: ${details.formalMode}`);
	if (details.formalStatus) lines.push(`formal status: ${details.formalStatus}`);
	if (
		details.formalDetail
		&& ["fail", "unknown", "error"].includes(details.formalStatus ?? "")
	) {
		lines.push(`formal detail: ${details.formalDetail.slice(0, 1200)}`);
	}
	if (
		details.formalArtifact
		&& ["fail", "unknown", "error"].includes(details.formalStatus ?? "")
	) {
		lines.push(`formal artifact: ${details.formalArtifact}`);
	}
	if (details.universalMode) lines.push(`universal mode: ${details.universalMode}`);
	if (details.universalStatus) lines.push(`universal status: ${details.universalStatus}`);
	if (
		details.universalPropertyCount !== undefined
		|| details.universalPassedCount !== undefined
	) {
		lines.push(
			`universal properties: ${details.universalPassedCount ?? "?"}`
			+ `/${details.universalPropertyCount ?? "?"} passed`,
		);
	}
	if ((details.universalFailedPropertyIds?.length ?? 0) > 0) {
		lines.push(
			`universal failed properties: ${details.universalFailedPropertyIds?.join(", ")}`,
		);
	}
	if (details.universalProof !== undefined) {
		lines.push(`universal proof: ${details.universalProof}`);
	}
	if (details.universalDetail) {
		lines.push(`universal detail: ${details.universalDetail.slice(0, 1200)}`);
	}
	if (details.universalArtifact) {
		lines.push(`universal artifact: ${details.universalArtifact}`);
	}
	if (details.verilatorStatus) lines.push(`Verilator status: ${details.verilatorStatus}`);
	if (details.counterexampleCount !== undefined) {
		lines.push(`counterexamples: ${details.counterexampleCount}`);
	}
	if (details.latestAttempt) lines.push(`attempt: ${details.latestAttempt}`);
	if ((details.architectureFailedChecks?.length ?? 0) > 0) {
		lines.push(
			`candidate architecture failed checks: ${details.architectureFailedChecks?.join(", ")}`,
		);
	}
	if (details.architectureArtifact) {
		lines.push(`candidate architecture evidence: ${details.architectureArtifact}`);
	}
}

function appendReleaseEvidenceLines(
	lines: string[],
	architectureAudit: Record<string, unknown>,
	synthesisEquivalence: Record<string, unknown>,
): void {
	const architectureStatus = stringValue(architectureAudit.status);
	if (architectureStatus) {
		const failed = strings(architectureAudit.failed_checks);
		lines.push(
			`architecture audit: ${architectureStatus}`
			+ (failed.length > 0 ? `, failed: ${failed.join(", ")}` : ""),
		);
		const artifact = stringValue(architectureAudit.artifact);
		if (artifact) lines.push(`architecture evidence: ${artifact}`);
	}
	const equivalenceStatus = stringValue(synthesisEquivalence.status);
	if (equivalenceStatus) {
		const cells = finiteNumber(synthesisEquivalence.equivalence_cells);
		const proven = finiteNumber(synthesisEquivalence.proven_cells);
		lines.push(
			`synthesis equivalence: ${equivalenceStatus}`
			+ (cells === undefined ? "" : `, proven ${proven ?? "?"}/${cells}`),
		);
		const artifact = stringValue(synthesisEquivalence.artifact);
		const log = stringValue(synthesisEquivalence.log);
		if (artifact) lines.push(`equivalence evidence: ${artifact}`);
		if (log) lines.push(`equivalence log: ${log}`);
	}
}

function eventRepairDetails(event: RtlClosureEvent): RepairDetails {
	const details = repairDetails(
		event.status,
		event.phase,
		event.repair ?? {},
		event.verification ?? {},
	);
	return {
		...details,
		universalMode: event.universalMode ?? details.universalMode,
		universalStatus: event.universalStatus ?? details.universalStatus,
		universalDetail: event.universalDetail ?? details.universalDetail,
		universalArtifact: event.universalArtifact ?? details.universalArtifact,
		universalPropertyCount:
			event.universalPropertyCount ?? details.universalPropertyCount,
		universalPassedCount: event.universalPassedCount ?? details.universalPassedCount,
		universalFailedPropertyIds:
			event.universalFailedPropertyIds ?? details.universalFailedPropertyIds,
		universalProof: event.universalProof ?? details.universalProof,
	};
}

function modelRequestEventLabel(event: ModelRequestEvent): string {
	switch (event) {
		case "request_started":
			return "started";
		case "request_waiting":
			return "waiting for stream";
		case "stream_connected":
			return "stream connected";
		case "streaming":
			return "stream heartbeat";
		case "request_completed":
			return "completed";
		case "request_retrying":
			return "retrying";
		case "request_failed":
			return "failed";
	}
}

function appendModelRequestLines(
	lines: string[],
	details: ModelRequestProgress | undefined,
): void {
	if (!details) return;
	const headline = [`model request: ${modelRequestEventLabel(details.event)}`];
	if (details.requestId !== undefined) headline.push(`request ${details.requestId}`);
	if (details.candidate !== undefined || details.candidateCount !== undefined) {
		headline.push(`candidate ${details.candidate ?? "?"}/${details.candidateCount ?? "?"}`);
	}
	if (details.attempt !== undefined || details.maxAttempts !== undefined) {
		headline.push(`attempt ${details.attempt ?? "?"}/${details.maxAttempts ?? "?"}`);
	}
	if (details.activeRequestCount !== undefined) {
		headline.push(`active ${details.activeRequestCount}`);
	}
	lines.push(headline.join(", "));

	const timing: string[] = [];
	if (details.elapsedSeconds !== undefined) {
		timing.push(`elapsed ${details.elapsedSeconds}s`);
	}
	if (details.firstEventSeconds !== undefined) {
		timing.push(`first event ${details.firstEventSeconds}s`);
	}
	if (details.chunkCount !== undefined) timing.push(`chunks ${details.chunkCount}`);
	if (timing.length > 0) lines.push(`model stream: ${timing.join(", ")}`);

	const retry: string[] = [];
	if (details.retryInSeconds !== undefined) {
		retry.push(`in ${details.retryInSeconds}s`);
	}
	if (details.retryAfterSeconds !== undefined) {
		retry.push(`provider delay ${details.retryAfterSeconds}s`);
	}
	if (retry.length > 0) lines.push(`model retry: ${retry.join(", ")}`);

	const failure: string[] = [];
	if (details.errorType) failure.push(details.errorType);
	if (details.httpStatus !== undefined) failure.push(`HTTP ${details.httpStatus}`);
	if (details.retryable !== undefined) failure.push(`retryable ${details.retryable}`);
	if (details.rayId) failure.push(`ray ${details.rayId}`);
	if (failure.length > 0) lines.push(`model error: ${failure.join(", ")}`);
}

export function eventSummary(runId: string, event: RtlClosureEvent): string {
	const lines = [
		`[Pi Lane] RTL-Closure transition #${event.sequence}`,
		`run: ${runId}`,
		`status: ${event.status}`,
		`phase: ${event.phase}`,
	];
	const verification = event.verification ?? {};
	const coverage = event.coverage ?? {};
	appendModelRequestLines(lines, event.modelRequest);
	appendRepairLines(
		lines,
		eventRepairDetails(event),
	);
	const coveragePercent = finiteNumber(coverage.coverage_percent);
	if (coveragePercent !== undefined) {
		const remaining = finiteNumber(coverage.remaining_cycle_budget);
		lines.push(
			`oracle coverage: ${coveragePercent}% / ${finiteNumber(coverage.coverage_target_pct) ?? "?"}%`
			+ `, round ${finiteNumber(coverage.round) ?? 0}/${finiteNumber(coverage.max_rounds) ?? "?"}`
			+ (remaining === undefined ? "" : `, remaining budget ${remaining} cycles`),
		);
	}
	const coverageStatus = stringValue(coverage.status);
	const targetMet = booleanValue(coverage.target_met);
	if (coverageStatus) {
		lines.push(
			`coverage status: ${coverageStatus}`
			+ (targetMet === undefined ? "" : `, target met: ${targetMet}`),
		);
	}
	const coverageWarning = stringValue(coverage.warning);
	const coverageError = stringValue(coverage.error);
	if (coverageWarning) lines.push(`coverage warning: ${coverageWarning}`);
	if (coverageError) lines.push(`coverage error: ${coverageError}`);
	appendReleaseEvidenceLines(
		lines,
		event.architectureAudit ?? {},
		event.synthesisEquivalence ?? {},
	);
	const passed = booleanValue(verification.passed);
	if (passed !== undefined) {
		lines.push(
			`verification: ${passed ? "passed" : "failed"}`
			+ ` (${finiteNumber(verification.total_checks) ?? 0} checks, `
			+ `${finiteNumber(verification.mismatched) ?? 0} mismatches)`,
		);
	}
	if (event.error) lines.push(`error: ${event.error}`);
	return lines.join("\n");
}

function formalNoticeType(
	formalMode: string | undefined,
	formalStatus: string | undefined,
	verilatorStatus?: string,
): NoticeType {
	const formalType: NoticeType = formalStatus === "unknown" || formalStatus === "error"
		? formalMode === "required" ? "error" : "warning"
		: formalStatus === "fail" ? "warning" : "info";
	const lintType: NoticeType = ["unknown", "error"].includes(verilatorStatus ?? "")
		? formalMode === "required" ? "error" : "warning"
		: verilatorStatus === "fail" ? "warning" : "info";
	return strongestNotice(formalType, lintType);
}

function universalNoticeType(
	mode: string | undefined,
	status: string | undefined,
): NoticeType {
	if (status === "fail") return "warning";
	if (status === "unknown" || status === "error") {
		return mode === "required" ? "error" : "warning";
	}
	return "info";
}

function strongestNotice(...types: NoticeType[]): NoticeType {
	if (types.includes("error")) return "error";
	if (types.includes("warning")) return "warning";
	return "info";
}

function modelRequestNoticeType(
	details: ModelRequestProgress | undefined,
): NoticeType {
	if (details?.event === "request_failed" || details?.event === "request_retrying") {
		return "warning";
	}
	return "info";
}

function coverageNoticeType(
	coverage: Record<string, unknown>,
	phase: string,
): NoticeType {
	const status = stringValue(coverage.status);
	const error = stringValue(coverage.error);
	if (status === "error" || error) {
		return phase === "coverage_refinement_error" ? "error" : "warning";
	}
	if (
		status === "warning"
		|| stringValue(coverage.warning)
		|| (booleanValue(coverage.target_met) === false && status !== "running")
	) {
		return "warning";
	}
	return "info";
}

export function eventNoticeType(event: RtlClosureEvent): NoticeType {
	if (event.status === "failed" || event.error) return "error";
	if (stringValue(event.architectureAudit?.status) === "fail") return "error";
	const details = eventRepairDetails(event);
	return strongestNotice(
		modelRequestNoticeType(event.modelRequest),
		coverageNoticeType(event.coverage ?? {}, event.phase),
		formalNoticeType(
			details.formalMode,
			details.formalStatus,
			details.verilatorStatus,
		),
		universalNoticeType(details.universalMode, details.universalStatus),
	);
}

export function fingerprint(snapshot: Snapshot): string {
	return JSON.stringify([
		snapshot.runId,
		snapshot.workflowRevision,
		snapshot.rtlClosureRunId,
		snapshot.status,
		snapshot.phase,
		snapshot.currentStep,
		snapshot.lastCompletedStep,
		snapshot.completedSteps,
		snapshot.errors,
		snapshot.visualizationStatus,
		snapshot.visualizationCount,
		snapshot.latestVisualization,
		snapshot.visualizationErrors,
		snapshot.qorGate,
		snapshot.signoffGate,
		snapshot.physicalSanityGate,
		snapshot.qorReasons,
		snapshot.processStatus,
		snapshot.flowScope,
		snapshot.deliveryGate,
		snapshot.deliveryReasons,
		snapshot.expectedFinalStep,
		snapshot.endpointReached,
		snapshot.mappedEquivalenceStatus,
		snapshot.mappedEquivalenceGate,
		snapshot.mappedEquivalenceScope,
		snapshot.mappedEquivalenceReasons,
		snapshot.openPdk,
		snapshot.foundrySignoff,
		snapshot.tapeoutGuarantee,
		snapshot.assuranceLimitations,
		snapshot.repairRound,
		snapshot.repairCandidate,
		snapshot.repairCandidateCount,
		snapshot.repairFailureClass,
		snapshot.repairRequirementId,
		snapshot.repairFirstFailureCycle,
		snapshot.repairStrategy,
		snapshot.repairCandidateResult,
		snapshot.repairCandidateEligible,
		snapshot.repairCandidateReason,
		snapshot.repairCandidateScore,
		snapshot.repairStagnation,
		snapshot.repairAction,
		snapshot.adaptiveAddedScenarios,
		snapshot.adaptiveEpoch,
		snapshot.suiteSha256,
		snapshot.formalMode,
		snapshot.formalStatus,
		snapshot.formalDetail,
		snapshot.formalArtifact,
		snapshot.universalMode,
		snapshot.universalStatus,
		snapshot.universalDetail,
		snapshot.universalArtifact,
		snapshot.universalPropertyCount,
		snapshot.universalPassedCount,
		snapshot.universalFailedPropertyIds,
		snapshot.universalProof,
		snapshot.verilatorStatus,
		snapshot.counterexampleCount,
		snapshot.latestAttempt,
		snapshot.verificationPassed,
		snapshot.verificationChecks,
		snapshot.verificationMismatches,
		snapshot.coverageRound,
		snapshot.coveragePercent,
		snapshot.coverageTargetPercent,
		snapshot.coverageRemainingCycles,
		snapshot.coverageStatus,
		snapshot.coverageTargetMet,
		snapshot.coverageWarning,
		snapshot.coverageError,
		snapshot.architectureAuditStatus,
		snapshot.architectureFailedChecks,
		snapshot.architectureAuditArtifact,
		snapshot.repairArchitectureFailedChecks,
		snapshot.repairArchitectureArtifact,
		snapshot.synthesisEquivalenceStatus,
		snapshot.synthesisEquivalenceCells,
		snapshot.synthesisEquivalenceProvenCells,
		snapshot.synthesisEquivalenceArtifact,
		snapshot.synthesisEquivalenceLog,
		snapshot.modelRequest,
		snapshot.failureCode,
		snapshot.failureDomain,
		snapshot.failureMessage,
		snapshot.failureRetryable,
		snapshot.failureRepairableByRtl,
		snapshot.failureUserActionRequired,
		snapshot.failureEvidence,
	]);
}

export function summary(snapshot: Snapshot): string {
	const kindLabel = snapshot.kind === "rtl_closure" ? "RTL-Closure" : snapshot.kind;
	const lines = [
		`[Pi Lane] ${kindLabel} update`,
		`run: ${snapshot.runId}`,
		`status: ${snapshot.status}`,
		`phase: ${snapshot.phase}`,
	];
	if (snapshot.workflowRevision !== undefined) {
		lines.push(`revision: ${snapshot.workflowRevision}`);
	}
	if (snapshot.rtlClosureRunId) {
		lines.push(`RTL-Closure run: ${snapshot.rtlClosureRunId}`);
	}
	if (snapshot.laneRunId) lines.push(`Lane run: ${snapshot.laneRunId}`);
	if (snapshot.failureCode || snapshot.failureMessage) {
		lines.push(
			`failure: ${snapshot.failureCode ?? "UNKNOWN"}`
			+ (snapshot.failureDomain ? ` (${snapshot.failureDomain})` : ""),
		);
		if (snapshot.failureMessage) lines.push(`failure detail: ${snapshot.failureMessage}`);
		lines.push(
			`failure policy: retryable ${snapshot.failureRetryable ?? "unknown"}, `
			+ `RTL repairable ${snapshot.failureRepairableByRtl ?? "unknown"}, `
			+ `user action ${snapshot.failureUserActionRequired ?? "unknown"}`,
		);
		if ((snapshot.failureEvidence?.length ?? 0) > 0) {
			lines.push(`failure evidence: ${snapshot.failureEvidence?.[0]}`);
		}
	}
	if (snapshot.currentStep) lines.push(`current: ${snapshot.currentStep}`);
	appendModelRequestLines(lines, snapshot.modelRequest);
	appendRepairLines(lines, {
		round: snapshot.repairRound,
		maxRounds: snapshot.repairMaxRounds,
		candidate: snapshot.repairCandidate,
		candidateCount: snapshot.repairCandidateCount,
		failureClass: snapshot.repairFailureClass,
		requirementId: snapshot.repairRequirementId,
		firstFailureCycle: snapshot.repairFirstFailureCycle,
		strategy: snapshot.repairStrategy,
		candidateResult: snapshot.repairCandidateResult,
		candidateEligible: snapshot.repairCandidateEligible,
		candidateReason: snapshot.repairCandidateReason,
		candidateScore: snapshot.repairCandidateScore,
		stagnation: snapshot.repairStagnation,
		action: snapshot.repairAction,
		adaptiveAddedScenarios: snapshot.adaptiveAddedScenarios,
		adaptiveEpoch: snapshot.adaptiveEpoch,
		suiteSha256: snapshot.suiteSha256,
		formalMode: snapshot.formalMode,
		formalStatus: snapshot.formalStatus,
		formalDetail: snapshot.formalDetail,
		formalArtifact: snapshot.formalArtifact,
		universalMode: snapshot.universalMode,
		universalStatus: snapshot.universalStatus,
		universalDetail: snapshot.universalDetail,
		universalArtifact: snapshot.universalArtifact,
		universalPropertyCount: snapshot.universalPropertyCount,
		universalPassedCount: snapshot.universalPassedCount,
		universalFailedPropertyIds: snapshot.universalFailedPropertyIds,
		universalProof: snapshot.universalProof,
		verilatorStatus: snapshot.verilatorStatus,
		counterexampleCount: snapshot.counterexampleCount,
		latestAttempt: snapshot.latestAttempt,
		architectureFailedChecks: snapshot.repairArchitectureFailedChecks,
		architectureArtifact: snapshot.repairArchitectureArtifact,
	});
	if (snapshot.coveragePercent !== undefined) {
		lines.push(
			`oracle coverage: ${snapshot.coveragePercent}% / ${snapshot.coverageTargetPercent ?? "?"}%`
			+ `, round ${snapshot.coverageRound ?? 0}/${snapshot.coverageMaxRounds ?? "?"}`
			+ (snapshot.coverageRemainingCycles === undefined
				? ""
				: `, remaining budget ${snapshot.coverageRemainingCycles} cycles`),
		);
	}
	if (snapshot.coverageStatus) {
		lines.push(
			`coverage status: ${snapshot.coverageStatus}`
			+ (snapshot.coverageTargetMet === undefined
				? ""
				: `, target met: ${snapshot.coverageTargetMet}`),
		);
	}
	if (snapshot.coverageWarning) {
		lines.push(`coverage warning: ${snapshot.coverageWarning}`);
	}
	if (snapshot.coverageError) lines.push(`coverage error: ${snapshot.coverageError}`);
	appendReleaseEvidenceLines(
		lines,
		{
			status: snapshot.architectureAuditStatus,
			failed_checks: snapshot.architectureFailedChecks,
			artifact: snapshot.architectureAuditArtifact,
		},
		{
			status: snapshot.synthesisEquivalenceStatus,
			equivalence_cells: snapshot.synthesisEquivalenceCells,
			proven_cells: snapshot.synthesisEquivalenceProvenCells,
			artifact: snapshot.synthesisEquivalenceArtifact,
			log: snapshot.synthesisEquivalenceLog,
		},
	);
	if (snapshot.verificationPassed !== undefined) {
		lines.push(
			`verification: ${snapshot.verificationPassed ? "passed" : "running/failed"}`
			+ ` (${snapshot.verificationChecks ?? 0} checks, `
			+ `${snapshot.verificationMismatches ?? 0} mismatches)`,
		);
	}
	if (snapshot.lastCompletedStep) {
		lines.push(`last completed: ${snapshot.lastCompletedStep}`);
	}
	if (snapshot.processStatus) lines.push(`process status: ${snapshot.processStatus}`);
	if (snapshot.flowScope || snapshot.deliveryGate) {
		lines.push(
			`delivery: ${snapshot.deliveryGate ?? "not_applicable"}`
			+ ` (scope ${snapshot.flowScope ?? "unknown"})`,
		);
	}
	if ((snapshot.deliveryReasons?.length ?? 0) > 0) {
		lines.push(`delivery reason: ${snapshot.deliveryReasons?.[0]}`);
	}
	if (snapshot.mappedEquivalenceStatus || snapshot.mappedEquivalenceGate) {
		lines.push(
			`mapped equivalence: ${snapshot.mappedEquivalenceStatus ?? "unknown"}`
			+ ` (gate ${snapshot.mappedEquivalenceGate ?? "unknown"})`,
		);
	}
	if (snapshot.mappedEquivalenceScope) {
		lines.push(`mapped equivalence scope: ${snapshot.mappedEquivalenceScope}`);
	}
	if ((snapshot.mappedEquivalenceReasons?.length ?? 0) > 0) {
		lines.push(`mapped equivalence reason: ${snapshot.mappedEquivalenceReasons?.[0]}`);
	}
	if (
		snapshot.openPdk !== undefined
		|| snapshot.foundrySignoff !== undefined
		|| snapshot.tapeoutGuarantee !== undefined
	) {
		lines.push(
			`assurance: open PDK ${snapshot.openPdk ?? "unknown"}`
			+ `, foundry signoff ${snapshot.foundrySignoff ?? "unknown"}`
			+ `, tapeout guarantee ${snapshot.tapeoutGuarantee ?? "unknown"}`,
		);
	}
	for (const limitation of snapshot.assuranceLimitations ?? []) {
		lines.push(`assurance limitation: ${limitation}`);
	}
	if (snapshot.qorGate) {
		lines.push(
			`QoR gate: ${snapshot.qorGate}`
			+ ` (LibreLane signoff-check gate ${snapshot.signoffGate ?? "unknown"}, `
			+ `physical sanity ${snapshot.physicalSanityGate ?? "unknown"})`,
		);
	}
	if ((snapshot.qorReasons?.length ?? 0) > 0) {
		lines.push(`QoR failure: ${snapshot.qorReasons?.[0]}`);
	}
	lines.push(`completed steps: ${snapshot.completedSteps}`);
	lines.push(`elapsed: ${snapshot.elapsedSeconds}s`);
	if (snapshot.errors.length > 0) lines.push(`error: ${snapshot.errors[0]}`);
	if (snapshot.visualizationStatus) {
		lines.push(
			`visualizations: ${snapshot.visualizationStatus} (${snapshot.visualizationCount ?? 0})`,
		);
	}
	if ((snapshot.visualizationErrors?.length ?? 0) > 0) {
		lines.push(`visualization error: ${snapshot.visualizationErrors?.[0]}`);
	}
	return lines.join("\n");
}

export function heartbeatSummary(snapshot: Snapshot): string {
	const lines = summary(snapshot).split("\n");
	const kindLabel = snapshot.kind === "rtl_closure" ? "RTL-Closure" : snapshot.kind;
	lines[0] = `[Pi Lane] ${kindLabel} heartbeat`;
	return lines.join("\n");
}

export function statusType(snapshot: Snapshot): NoticeType {
	if (snapshot.status === "failed" || snapshot.errors.length > 0) return "error";
	if (snapshot.status === "blocked_infrastructure") return "error";
	if (snapshot.deliveryGate === "fail") return "error";
	if (snapshot.architectureAuditStatus === "fail") return "error";
	if (snapshot.qorGate === "fail") return "error";
	const verificationType = strongestNotice(
		modelRequestNoticeType(snapshot.modelRequest),
		coverageNoticeType(
			{
				status: snapshot.coverageStatus,
				target_met: snapshot.coverageTargetMet,
				warning: snapshot.coverageWarning,
				error: snapshot.coverageError,
			},
			snapshot.phase,
		),
		formalNoticeType(
			snapshot.formalMode,
			snapshot.formalStatus,
			snapshot.verilatorStatus,
		),
		universalNoticeType(snapshot.universalMode, snapshot.universalStatus),
	);
	if (verificationType !== "info") return verificationType;
	if (
		snapshot.status === "stopped"
		|| snapshot.status === "cancelled"
		|| snapshot.status === "waiting_user"
		|| snapshot.status === "incomplete"
		|| snapshot.deliveryGate === "incomplete"
		|| (snapshot.visualizationErrors?.length ?? 0) > 0
		|| (snapshot.status === "completed" && snapshot.qorGate === "unknown")
	) {
		return "warning";
	}
	return "info";
}

export function widgetLines(snapshot: Snapshot): string[] {
	const progress = snapshot.currentStep ?? snapshot.phase;
	const kindLabel = snapshot.kind === "rtl_closure" ? "RTL-Closure" : snapshot.kind;
	const lines = [
		`Pi Lane monitor | ${snapshot.status} | ${kindLabel} | ${progress}`,
		`run ${snapshot.runId} | completed ${snapshot.completedSteps} | ${snapshot.elapsedSeconds}s`,
	];
	if (snapshot.workflowRevision !== undefined) {
		lines.push(
			`revision ${snapshot.workflowRevision}`
			+ (snapshot.rtlClosureRunId
				? ` | RTL-Closure ${snapshot.rtlClosureRunId}`
				: "")
			+ (snapshot.laneRunId ? ` | Lane ${snapshot.laneRunId}` : ""),
		);
	}
	if (snapshot.failureCode || snapshot.failureMessage) {
		lines.push(
			`failure ${snapshot.failureCode ?? "UNKNOWN"}`
			+ (snapshot.failureDomain ? ` | ${snapshot.failureDomain}` : "")
			+ (snapshot.failureRetryable === undefined
				? ""
				: ` | retryable ${snapshot.failureRetryable}`),
		);
		if (snapshot.failureMessage) {
			lines.push(`failure detail ${snapshot.failureMessage.slice(0, 240)}`);
		}
	}
	appendModelRequestLines(lines, snapshot.modelRequest);
	if (snapshot.repairRound !== undefined) {
		lines.push(
			`repair ${snapshot.repairRound}/${snapshot.repairMaxRounds ?? "?"}`
			+ ` | candidate ${snapshot.repairCandidate ?? 0}/${snapshot.repairCandidateCount ?? 0}`
			+ ` | counterexamples ${snapshot.counterexampleCount ?? 0}`,
		);
	}
	const context = [snapshot.repairRequirementId, snapshot.repairStrategy]
		.filter((item) => item !== undefined)
		.join(" | ");
	if (context) lines.push(context);
	if (snapshot.repairFailureClass) {
		lines.push(
			`failure ${snapshot.repairFailureClass}`
			+ (snapshot.repairFirstFailureCycle !== undefined
				? ` | cycle ${snapshot.repairFirstFailureCycle}`
				: ""),
		);
	}
	if (snapshot.repairCandidateResult || snapshot.repairAction) {
		lines.push(
			[
				snapshot.repairCandidateResult
					? `result ${snapshot.repairCandidateResult}`
					: undefined,
				snapshot.repairCandidateScore !== undefined
					? `score ${scoreText(snapshot.repairCandidateScore)}`
					: undefined,
				snapshot.repairStagnation !== undefined
					? `stagnation ${snapshot.repairStagnation}`
					: undefined,
				snapshot.repairAction ? `action ${snapshot.repairAction}` : undefined,
			].filter((item) => item !== undefined).join(" | "),
		);
	}
	if (snapshot.coveragePercent !== undefined) {
		lines.push(
			`oracle coverage ${snapshot.coveragePercent}%/${snapshot.coverageTargetPercent ?? "?"}%`
			+ ` | round ${snapshot.coverageRound ?? 0}/${snapshot.coverageMaxRounds ?? "?"}`
			+ (snapshot.coverageRemainingCycles === undefined
				? ""
				: ` | budget ${snapshot.coverageRemainingCycles} cycles`),
		);
	}
	if (snapshot.coverageStatus) {
		lines.push(
			`coverage ${snapshot.coverageStatus}`
			+ (snapshot.coverageTargetMet === undefined
				? ""
				: ` | target met ${snapshot.coverageTargetMet}`),
		);
	}
	if (snapshot.coverageWarning) {
		lines.push(`coverage warning ${snapshot.coverageWarning.slice(0, 240)}`);
	}
	if (snapshot.coverageError) {
		lines.push(`coverage error ${snapshot.coverageError.slice(0, 240)}`);
	}
	if (snapshot.formalStatus || snapshot.verilatorStatus) {
		lines.push(
			`formal ${snapshot.formalStatus ?? "running"}`
			+ ` | Verilator ${snapshot.verilatorStatus ?? "running"}`,
		);
	}
	if (snapshot.universalMode || snapshot.universalStatus) {
		lines.push(
			`universal ${snapshot.universalStatus ?? "running"}`
			+ (snapshot.universalMode ? ` | mode ${snapshot.universalMode}` : "")
			+ (
				snapshot.universalPropertyCount === undefined
				&& snapshot.universalPassedCount === undefined
					? ""
					: ` | passed ${snapshot.universalPassedCount ?? "?"}/${snapshot.universalPropertyCount ?? "?"}`
			)
			+ (snapshot.universalProof === undefined
				? ""
				: ` | proof ${snapshot.universalProof}`),
		);
	}
	if ((snapshot.universalFailedPropertyIds?.length ?? 0) > 0) {
		lines.push(`universal failed ${snapshot.universalFailedPropertyIds?.join(", ")}`);
	}
	if (
		snapshot.universalDetail
		&& ["fail", "unknown", "error"].includes(snapshot.universalStatus ?? "")
	) {
		lines.push(`universal detail ${snapshot.universalDetail.slice(0, 240)}`);
	}
	if (snapshot.universalArtifact) {
		lines.push(`universal evidence ${snapshot.universalArtifact}`);
	}
	if ((snapshot.repairArchitectureFailedChecks?.length ?? 0) > 0) {
		lines.push(
			`candidate architecture failed ${snapshot.repairArchitectureFailedChecks?.join(", ")}`,
		);
	}
	if (snapshot.repairArchitectureArtifact) {
		lines.push(`candidate architecture evidence ${snapshot.repairArchitectureArtifact}`);
	}
	if (snapshot.architectureAuditStatus) {
		lines.push(
			`architecture ${snapshot.architectureAuditStatus}`
			+ ((snapshot.architectureFailedChecks?.length ?? 0) > 0
				? ` | failed ${snapshot.architectureFailedChecks?.join(", ")}`
				: ""),
		);
	}
	if (snapshot.synthesisEquivalenceStatus) {
		lines.push(
			`equivalence ${snapshot.synthesisEquivalenceStatus}`
			+ (snapshot.synthesisEquivalenceCells === undefined
				? ""
				: ` | proven ${snapshot.synthesisEquivalenceProvenCells ?? "?"}/${snapshot.synthesisEquivalenceCells}`),
		);
	}
	if (
		snapshot.formalDetail
		&& ["fail", "unknown", "error"].includes(snapshot.formalStatus ?? "")
	) {
		lines.push(`formal detail ${snapshot.formalDetail.slice(0, 240)}`);
	}
	if (snapshot.processStatus || snapshot.deliveryGate) {
		lines.push(
			`process ${snapshot.processStatus ?? "unknown"}`
			+ ` | scope ${snapshot.flowScope ?? "unknown"}`
			+ ` | delivery ${snapshot.deliveryGate ?? "not_applicable"}`,
		);
	}
	if ((snapshot.deliveryReasons?.length ?? 0) > 0) {
		lines.push(`delivery reason ${snapshot.deliveryReasons?.[0].slice(0, 240)}`);
	}
	if (snapshot.mappedEquivalenceStatus || snapshot.mappedEquivalenceGate) {
		lines.push(
			`mapped equivalence ${snapshot.mappedEquivalenceStatus ?? "unknown"}`
			+ ` | gate ${snapshot.mappedEquivalenceGate ?? "unknown"}`,
		);
	}
	if (snapshot.mappedEquivalenceScope) {
		lines.push(`mapped equivalence scope ${snapshot.mappedEquivalenceScope.slice(0, 240)}`);
	}
	if ((snapshot.mappedEquivalenceReasons?.length ?? 0) > 0) {
		lines.push(
			`mapped equivalence reason ${snapshot.mappedEquivalenceReasons?.[0].slice(0, 240)}`,
		);
	}
	if (
		snapshot.openPdk !== undefined
		|| snapshot.foundrySignoff !== undefined
		|| snapshot.tapeoutGuarantee !== undefined
	) {
		lines.push(
			`assurance open PDK ${snapshot.openPdk ?? "unknown"}`
			+ ` | foundry signoff ${snapshot.foundrySignoff ?? "unknown"}`
			+ ` | tapeout guarantee ${snapshot.tapeoutGuarantee ?? "unknown"}`,
		);
	}
	if ((snapshot.assuranceLimitations?.length ?? 0) > 0) {
		lines.push(`assurance limitation ${snapshot.assuranceLimitations?.[0].slice(0, 240)}`);
	}
	if (snapshot.errors.length > 0) lines.push(`error: ${snapshot.errors[0]}`);
	if (snapshot.qorGate) {
		lines.push(
			`QoR ${snapshot.qorGate}`
			+ ` | LibreLane checks ${snapshot.signoffGate ?? "unknown"}`
			+ ` | sanity ${snapshot.physicalSanityGate ?? "unknown"}`,
		);
	}
	if ((snapshot.qorReasons?.length ?? 0) > 0) {
		lines.push(`QoR failure ${snapshot.qorReasons?.[0].slice(0, 240)}`);
	}
	if (snapshot.visualizationStatus) {
		lines.push(`previews ${snapshot.visualizationCount ?? 0} | ${snapshot.visualizationStatus}`);
	}
	return lines;
}

export function stepRecords(value: unknown): StepRecord[] {
	if (!Array.isArray(value)) return [];
	return value.flatMap((item) => {
		const raw = record(item);
		if (!raw) return [];
		const ordinal = finiteNumber(raw.ordinal);
		const id = stringValue(raw.id);
		if (ordinal === undefined || id === undefined) return [];
		return [{
			ordinal,
			id,
			phase: stringValue(raw.phase) ?? "implementation",
			status: stringValue(raw.status) ?? "unknown",
			directory: stringValue(raw.directory) ?? "",
			commands: stringValue(raw.commands),
			logs: strings(raw.logs),
			runtime: stringValue(raw.runtime),
			visualizationStatus: stringValue(raw.visualization_status) ?? "not_applicable",
			visualization: stringValue(raw.visualization),
			visualizationError: stringValue(raw.visualization_error),
		}];
	});
}

export function laneTimelineEvents(value: unknown): LaneTimelineEvent[] {
	if (!Array.isArray(value)) return [];
	return value.flatMap((item) => {
		const raw = record(item);
		if (!raw) return [];
		const sequence = finiteNumber(raw.sequence);
		const event = boundedIdentifier(raw.event);
		if (
			sequence === undefined
			|| !Number.isInteger(sequence)
			|| sequence < 1
			|| event === undefined
		) {
			return [];
		}
		const ordinal = finiteNumber(raw.ordinal);
		return [{
			sequence,
			event,
			timestamp: stringValue(raw.timestamp),
			phase: stringValue(raw.phase),
			status: stringValue(raw.status),
			severity: ["info", "warning", "error"].includes(stringValue(raw.severity) ?? "")
				? stringValue(raw.severity) as NoticeType
				: undefined,
			message: stringValue(raw.message),
			ordinal: ordinal !== undefined && Number.isInteger(ordinal) && ordinal > 0
				? ordinal
				: undefined,
			step: stringValue(raw.step),
			directory: stringValue(raw.directory),
			commands: stringValue(raw.commands),
			logs: strings(raw.logs),
			image: stringValue(raw.image),
			error: stringValue(raw.error),
			attempt: nonNegativeNumber(raw.attempt),
		}];
	});
}

export function initialLaneEventCursor(suppressHistory = true): LaneEventCursor {
	return {
		afterSequence: 0,
		cursorSha256: "e3b0c44298fc1c149afbf4c8996fb924"
			+ "27ae41e4649b934ca495991b7852b855",
		suppressHistory,
	};
}

function laneCursor(value: unknown): LaneEventCursor | undefined {
	const raw = record(value);
	if (!raw) return undefined;
	const afterSequence = finiteNumber(raw.afterSequence ?? raw.after_sequence);
	const generation = stringValue(raw.generation);
	const cursorSha256 = stringValue(raw.cursorSha256 ?? raw.cursor_sha256);
	const suppressHistory = booleanValue(raw.suppressHistory ?? raw.suppress_history);
	if (
		afterSequence === undefined
		|| !Number.isInteger(afterSequence)
		|| afterSequence < 0
		|| !cursorSha256
		|| !/^[0-9a-f]{64}$/.test(cursorSha256)
		|| suppressHistory === undefined
		|| (generation !== undefined && !/^[0-9a-f]+:[0-9a-f]+$/.test(generation))
		|| (afterSequence > 0 && generation === undefined)
	) {
		return undefined;
	}
	return { afterSequence, generation, cursorSha256, suppressHistory };
}

export function laneEventReport(
	raw: Record<string, unknown>,
	requested: LaneEventCursor = initialLaneEventCursor(),
): LaneEventReport | undefined {
	const runId = monitorRunId(raw.run_id);
	const snapshotRaw = record(raw.snapshot);
	const afterSequence = finiteNumber(raw.after_sequence);
	const nextAfterSequence = finiteNumber(raw.next_after_sequence);
	const generation = stringValue(raw.generation);
	const cursorSha256 = stringValue(raw.cursor_sha256);
	const hasMore = booleanValue(raw.has_more);
	const invalidLineCount = finiteNumber(raw.invalid_line_count);
	if (
		!runId
		|| !snapshotRaw
		|| afterSequence === undefined
		|| !Number.isInteger(afterSequence)
		|| afterSequence !== requested.afterSequence
		|| nextAfterSequence === undefined
		|| !Number.isInteger(nextAfterSequence)
		|| nextAfterSequence < afterSequence
		|| !cursorSha256
		|| !/^[0-9a-f]{64}$/.test(cursorSha256)
		|| (generation !== undefined && !/^[0-9a-f]+:[0-9a-f]+$/.test(generation))
		|| (nextAfterSequence > 0 && generation === undefined)
		|| (
			requested.generation !== undefined
			&& generation !== requested.generation
		)
		|| hasMore === undefined
		|| invalidLineCount === undefined
		|| !Number.isInteger(invalidLineCount)
		|| invalidLineCount < 0
	) {
		return undefined;
	}
	const snapshot = laneSnapshot({ ...snapshotRaw, run_id: runId });
	if (!snapshot) return undefined;
	const rawEvents = Array.isArray(raw.events) ? raw.events : undefined;
	const events = laneTimelineEvents(raw.events);
	if (!rawEvents || events.length !== rawEvents.length) return undefined;
	for (let index = 0; index < events.length; index += 1) {
		if (events[index].sequence !== afterSequence + index + 1) return undefined;
	}
	const lastSequence = events.at(-1)?.sequence ?? afterSequence;
	if (lastSequence !== nextAfterSequence) return undefined;
	if (events.length === 0 && cursorSha256 !== requested.cursorSha256) return undefined;
	if (hasMore && events.length === 0) return undefined;
	return {
		runId,
		events,
		snapshot,
		nextCursor: {
			afterSequence: nextAfterSequence,
			generation,
			cursorSha256,
			suppressHistory: false,
		},
		hasMore,
		invalidLineCount,
	};
}

export function workflowSnapshot(raw: Record<string, unknown>): Snapshot | undefined {
	const workflowId = monitorRunId(raw.workflow_id);
	const status = stringValue(raw.status);
	const phase = stringValue(raw.phase);
	const revision = nonNegativeNumber(raw.revision);
	if (!workflowId || !status || !phase || revision === undefined || !Number.isInteger(revision)) {
		return undefined;
	}
	const base = rtlClosureSnapshot({
		...raw,
		run_id: workflowId,
		lane_run_id: raw.lane_run_id,
	});
	if (!base) return undefined;
	const parsedFailure = record(raw.failure);
	if (raw.failure !== null && raw.failure !== undefined && !parsedFailure) return undefined;
	const failure = parsedFailure ?? {};
	const qor = record(raw.qor) ?? {};
	const physicalSanity = record(qor.physical_sanity) ?? {};
	return {
		...base,
		kind: "workflow",
		rtlClosureRunId: monitorRunId(raw.rtl_closure_run_id),
		laneRunId: monitorRunId(raw.lane_run_id),
		workflowRevision: revision,
		failureCode: boundedIdentifier(failure.code),
		failureDomain: boundedIdentifier(failure.domain),
		failureMessage: stringValue(failure.message),
		failureRetryable: booleanValue(failure.retryable),
		failureRepairableByRtl: booleanValue(failure.repairable_by_rtl),
		failureUserActionRequired: booleanValue(failure.user_action_required),
		failureEvidence: strings(failure.evidence),
		qorGate: stringValue(qor.gate),
		signoffGate: stringValue(qor.signoff_gate),
		physicalSanityGate: stringValue(physicalSanity.gate),
		qorReasons: strings(qor.gate_reasons),
	};
}

export function workflowEvents(value: unknown): WorkflowEvent[] {
	if (!Array.isArray(value)) return [];
	const events = value.flatMap((item) => {
		const raw = record(item);
		if (!raw) return [];
		const sequence = nonNegativeNumber(raw.sequence);
		const eventIdRaw = stringValue(raw.event_id);
		const eventId = eventIdRaw && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(eventIdRaw)
			? eventIdRaw
			: undefined;
		const timestamp = stringValue(raw.timestamp);
		const source = boundedIdentifier(raw.source);
		const type = boundedIdentifier(raw.type);
		const severityValue = stringValue(raw.severity);
		const stateRevision = nonNegativeNumber(raw.state_revision);
		const phase = stringValue(raw.phase);
		const payload = record(raw.payload);
		const attemptId = raw.attempt_id === null || raw.attempt_id === undefined
			? undefined
			: monitorRunId(raw.attempt_id);
		const failure = raw.failure === null || raw.failure === undefined
			? undefined
			: record(raw.failure);
		const previousSha256 = raw.previous_sha256 === null
			? undefined
			: stringValue(raw.previous_sha256);
		const sha256 = stringValue(raw.sha256);
		if (
			sequence === undefined
			|| !Number.isInteger(sequence)
			|| sequence < 1
			|| !eventId
			|| !timestamp
			|| !source
			|| !type
			|| !severityValue
			|| !["info", "warning", "error"].includes(severityValue)
			|| stateRevision === undefined
			|| !Number.isInteger(stateRevision)
			|| !phase
			|| !payload
			|| (raw.attempt_id !== null && raw.attempt_id !== undefined && !attemptId)
			|| (raw.failure !== null && raw.failure !== undefined && !failure)
			|| (previousSha256 !== undefined && !/^[0-9a-f]{64}$/.test(previousSha256))
			|| !sha256
			|| !/^[0-9a-f]{64}$/.test(sha256)
		) {
			return [];
		}
		return [{
			sequence,
			eventId,
			timestamp,
			source,
			attemptId,
			type,
			severity: severityValue as NoticeType,
			stateRevision,
			phase,
			payload,
			failure,
			previousSha256,
			sha256,
		}];
	});
	return events;
}

export function initialWorkflowEventCursor(suppressHistory = true): WorkflowEventCursor {
	return { afterSequence: 0, suppressHistory };
}

function workflowCursor(value: unknown): WorkflowEventCursor | undefined {
	const raw = record(value);
	if (!raw) return undefined;
	const afterSequence = nonNegativeNumber(raw.afterSequence ?? raw.after_sequence);
	const cursorSha256 = stringValue(raw.cursorSha256 ?? raw.cursor_sha256);
	const suppressHistory = booleanValue(raw.suppressHistory ?? raw.suppress_history);
	if (
		afterSequence === undefined
		|| !Number.isInteger(afterSequence)
		|| (cursorSha256 !== undefined && !/^[0-9a-f]{64}$/.test(cursorSha256))
		|| (afterSequence > 0 && cursorSha256 === undefined)
		|| suppressHistory === undefined
	) {
		return undefined;
	}
	return { afterSequence, cursorSha256, suppressHistory };
}

export function workflowEventReport(raw: Record<string, unknown>): WorkflowEventReport | undefined {
	const workflowId = monitorRunId(raw.workflow_id);
	const afterSequence = nonNegativeNumber(raw.after_sequence);
	const nextAfterSequence = nonNegativeNumber(raw.next_after_sequence);
	const hasMore = booleanValue(raw.has_more);
	const cursorSha256 = raw.cursor_sha256 === null
		? undefined
		: stringValue(raw.cursor_sha256);
	const acknowledgedSequence = raw.acknowledged_sequence === null
		? undefined
		: nonNegativeNumber(raw.acknowledged_sequence);
	const snapshotRaw = record(raw.snapshot);
	if (
		!workflowId
		|| afterSequence === undefined
		|| !Number.isInteger(afterSequence)
		|| nextAfterSequence === undefined
		|| !Number.isInteger(nextAfterSequence)
		|| nextAfterSequence < afterSequence
		|| hasMore === undefined
		|| !cursorSha256
		|| !/^[0-9a-f]{64}$/.test(cursorSha256)
		|| (
			acknowledgedSequence !== undefined
			&& !Number.isInteger(acknowledgedSequence)
		)
		|| !snapshotRaw
	) {
		return undefined;
	}
	const rawEvents = Array.isArray(raw.events) ? raw.events : undefined;
	const events = workflowEvents(raw.events);
	if (!rawEvents || events.length !== rawEvents.length) return undefined;
	for (let index = 0; index < events.length; index += 1) {
		if (events[index].sequence !== afterSequence + index + 1) return undefined;
		if (
			index > 0
			&& events[index].previousSha256 !== events[index - 1].sha256
		) {
			return undefined;
		}
	}
	const last = events.at(-1);
	if (
		(last ? last.sequence : afterSequence) !== nextAfterSequence
		|| (last !== undefined && last.sha256 !== cursorSha256)
	) {
		return undefined;
	}
	const snapshot = workflowSnapshot(snapshotRaw);
	if (!snapshot || snapshot.runId !== workflowId) return undefined;
	return {
		workflowId,
		events,
		snapshot,
		nextCursor: {
			afterSequence: nextAfterSequence,
			cursorSha256,
			suppressHistory: false,
		},
		hasMore,
		acknowledgedSequence,
	};
}

export function workflowEventNoticeType(event: WorkflowEvent): NoticeType {
	if (event.failure || event.severity === "error") return "error";
	return event.severity;
}

export function workflowEventSummary(workflowId: string, event: WorkflowEvent): string {
	const lines = [
		`[Pi Lane] workflow event #${event.sequence}`,
		`workflow: ${workflowId}`,
		`type: ${event.type}`,
		`phase: ${event.phase}`,
		`revision: ${event.stateRevision}`,
	];
	if (event.attemptId) lines.push(`attempt: ${event.attemptId}`);
	for (const [key, label] of [
		["status", "status"],
		["backend_run_id", "backend run"],
		["ordinal", "attempt ordinal"],
		["supersedes_attempt_id", "supersedes"],
	] as const) {
		const value = event.payload[key];
		if (typeof value === "string" || typeof value === "number") {
			lines.push(`${label}: ${value}`);
		}
	}
	const failure = event.failure ?? {};
	const code = boundedIdentifier(failure.code);
	const domain = boundedIdentifier(failure.domain);
	const message = stringValue(failure.message);
	if (code || domain) {
		lines.push(`failure: ${code ?? "UNKNOWN"}${domain ? ` (${domain})` : ""}`);
	}
	if (message) lines.push(`failure detail: ${message}`);
	return lines.join("\n");
}

export function monitorCheckpoint(value: unknown): MonitorCheckpoint | undefined {
	const raw = record(value);
	if (!raw || finiteNumber(raw.schemaVersion) !== 6) {
		return undefined;
	}
	const bindingsRaw = record(raw.bindings);
	const workflowRaw = record(raw.workflowEventCursors);
	const rtlClosureRaw = record(raw.rtlClosureEventCursors);
	const laneRaw = record(raw.laneEventCursors);
	const workflowAutoDiscovery = booleanValue(raw.workflowAutoDiscovery);
	if (
		!bindingsRaw
		|| !workflowRaw
		|| !rtlClosureRaw
		|| !laneRaw
		|| workflowAutoDiscovery === undefined
		|| !Array.isArray(raw.pinnedSources)
	) {
		return undefined;
	}
	const bindings: Partial<Record<MonitorSource, string>> = {};
	for (const source of ["workflow", "rtl_closure", "lane", "pdk", "image"] as const) {
		const runId = monitorRunId(bindingsRaw[source]);
		if (runId) bindings[source] = runId;
	}
	const clientIdRaw = stringValue(raw.clientId);
	const clientId = clientIdRaw && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(clientIdRaw)
		? clientIdRaw
		: undefined;
	if (raw.clientId !== undefined && !clientId) return undefined;
	const workflowClaim = monitorRunId(raw.workflowClaim);
	if (raw.workflowClaim !== undefined && !workflowClaim) return undefined;
	const recentTerminalWorkflow = monitorRunId(raw.recentTerminalWorkflow);
	if (raw.recentTerminalWorkflow !== undefined && !recentTerminalWorkflow) return undefined;
	const pinnedSources: MonitorSource[] = [];
	for (const value of raw.pinnedSources.slice(0, 5)) {
		if (
			typeof value !== "string"
			|| !["workflow", "rtl_closure", "lane", "pdk", "image"].includes(value)
		) {
			return undefined;
		}
		if (!pinnedSources.includes(value as MonitorSource)) {
			pinnedSources.push(value as MonitorSource);
		}
	}
	const workflowEventCursors: Record<string, WorkflowEventCursor> = {};
	const protectedRunIds = new Set([
		...Object.values(bindings),
		...(workflowClaim ? [workflowClaim] : []),
		...(recentTerminalWorkflow ? [recentTerminalWorkflow] : []),
	]);
	const boundedEntries = (value: Record<string, unknown>): [string, unknown][] => {
		const all = Object.entries(value);
		const selected = all.slice(-256);
		for (const runId of protectedRunIds) {
			const entry = all.find(([candidate]) => candidate === runId);
			if (!entry || selected.some(([candidate]) => candidate === runId)) continue;
			const removable = selected.findIndex(([candidate]) => !protectedRunIds.has(candidate));
			if (selected.length >= 256) selected.splice(Math.max(0, removable), 1);
			selected.push(entry);
		}
		return selected;
	};
	for (const [runIdValue, cursorValue] of boundedEntries(workflowRaw)) {
		const runId = monitorRunId(runIdValue);
		const cursor = workflowCursor(cursorValue);
		if (runId && cursor) workflowEventCursors[runId] = cursor;
	}

	const rtlClosureEventCursors: Record<string, number> = {};
	for (const [runIdValue, cursorValue] of boundedEntries(rtlClosureRaw)) {
		const runId = monitorRunId(runIdValue);
		const cursor = finiteNumber(cursorValue);
		if (runId && cursor !== undefined && Number.isInteger(cursor) && cursor >= 0) {
			rtlClosureEventCursors[runId] = cursor;
		}
	}

	const laneEventCursors: Record<string, LaneEventCursor> = {};
	for (const [runIdValue, cursorValue] of boundedEntries(laneRaw)) {
		const runId = monitorRunId(runIdValue);
		const cursor = laneCursor(cursorValue);
		if (runId && cursor) laneEventCursors[runId] = cursor;
	}
	return {
		schemaVersion: 6,
		clientId,
		workflowAutoDiscovery,
		workflowClaim,
		recentTerminalWorkflow,
		bindings,
		pinnedSources,
		workflowEventCursors,
		rtlClosureEventCursors,
		laneEventCursors,
	};
}

export function rtlClosureSnapshot(raw: Record<string, unknown>): Snapshot | undefined {
	const runId = stringValue(raw.run_id);
	const status = stringValue(raw.status);
	if (!runId || !status) return undefined;
	const phase = stringValue(raw.phase) ?? "unknown";
	const repair = record(raw.repair) ?? {};
	const verification = record(raw.verification) ?? {};
	const coverage = record(raw.coverage) ?? {};
	const architectureAudit = record(raw.architecture_audit) ?? {};
	const synthesisEquivalence = record(raw.synthesis_equivalence) ?? {};
	const details = repairDetails(status, phase, repair, verification);
	return {
		kind: "rtl_closure",
		runId,
		status,
		phase,
		currentStep: stringValue(raw.current_step),
		lastCompletedStep: stringValue(raw.last_completed_step),
		completedSteps: finiteNumber(raw.completed_step_count) ?? 0,
		elapsedSeconds: finiteNumber(raw.elapsed_seconds) ?? 0,
		errors: strings(raw.errors),
		laneRunId: stringValue(raw.lane_run_id),
		processStatus: stringValue(raw.process_status),
		flowScope: stringValue(raw.flow_scope),
		deliveryGate: stringValue(raw.delivery_gate),
		deliveryReasons: strings(raw.delivery_gate_reasons),
		expectedFinalStep: stringValue(raw.expected_final_step),
		endpointReached: booleanValue(raw.endpoint_reached),
		repairRound: details.round,
		repairMaxRounds: details.maxRounds,
		repairCandidate: details.candidate,
		repairCandidateCount: details.candidateCount,
		repairFailureClass: details.failureClass,
		repairRequirementId: details.requirementId,
		repairFirstFailureCycle: details.firstFailureCycle,
		repairStrategy: details.strategy,
		repairCandidateResult: details.candidateResult,
		repairCandidateEligible: details.candidateEligible,
		repairCandidateReason: details.candidateReason,
		repairCandidateScore: details.candidateScore,
		repairStagnation: details.stagnation,
		repairAction: details.action,
		adaptiveAddedScenarios: details.adaptiveAddedScenarios,
		adaptiveEpoch: details.adaptiveEpoch,
		suiteSha256: details.suiteSha256,
		formalMode: details.formalMode,
		formalStatus: details.formalStatus,
		formalDetail: details.formalDetail,
		formalArtifact: details.formalArtifact,
		universalMode: details.universalMode,
		universalStatus: details.universalStatus,
		universalDetail: details.universalDetail,
		universalArtifact: details.universalArtifact,
		universalPropertyCount: details.universalPropertyCount,
		universalPassedCount: details.universalPassedCount,
		universalFailedPropertyIds: details.universalFailedPropertyIds,
		universalProof: details.universalProof,
		verilatorStatus: details.verilatorStatus,
		counterexampleCount: details.counterexampleCount,
		latestAttempt: details.latestAttempt,
		verificationPassed: booleanValue(verification.passed),
		verificationChecks: finiteNumber(verification.total_checks),
		verificationMismatches: finiteNumber(verification.mismatched),
		coverageRound: finiteNumber(coverage.round),
		coverageMaxRounds: finiteNumber(coverage.max_rounds),
		coveragePercent: finiteNumber(coverage.coverage_percent),
		coverageTargetPercent: finiteNumber(coverage.coverage_target_pct),
		coverageRemainingCycles: finiteNumber(coverage.remaining_cycle_budget),
		coverageStatus: stringValue(coverage.status),
		coverageTargetMet: booleanValue(coverage.target_met),
		coverageWarning: stringValue(coverage.warning),
		coverageError: stringValue(coverage.error),
		architectureAuditStatus: stringValue(architectureAudit.status),
		architectureFailedChecks: strings(architectureAudit.failed_checks),
		architectureAuditArtifact: stringValue(architectureAudit.artifact),
		repairArchitectureFailedChecks: details.architectureFailedChecks,
		repairArchitectureArtifact: details.architectureArtifact,
		synthesisEquivalenceStatus: stringValue(synthesisEquivalence.status),
		synthesisEquivalenceCells: finiteNumber(
			synthesisEquivalence.equivalence_cells,
		),
		synthesisEquivalenceProvenCells: finiteNumber(
			synthesisEquivalence.proven_cells,
		),
		synthesisEquivalenceArtifact: stringValue(synthesisEquivalence.artifact),
		synthesisEquivalenceLog: stringValue(synthesisEquivalence.log),
		modelRequest: modelRequestProgress(raw.model_request),
		events: rtlClosureEvents(raw.events),
	};
}

export function laneSnapshot(raw: Record<string, unknown>): Snapshot | undefined {
	const runId = stringValue(raw.run_id);
	const status = stringValue(raw.status);
	if (!runId || !status) return undefined;
	const visualization = record(raw.visualization) ?? {};
	const qor = record(raw.qor) ?? {};
	const physicalSanity = record(qor.physical_sanity) ?? {};
	const mappedEquivalence = record(raw.mapped_equivalence) ?? {};
	const assurance = record(raw.assurance) ?? {};
	return {
		kind: "lane",
		runId,
		status,
		phase: firstString(raw.coarse_phase, raw.status) ?? status,
		currentStep: stringValue(raw.active_step),
		lastCompletedStep: stringValue(raw.last_completed_step),
		completedSteps: finiteNumber(raw.completed_step_count) ?? 0,
		elapsedSeconds: finiteNumber(raw.elapsed_seconds) ?? 0,
		errors: strings(raw.errors),
		processStatus: stringValue(raw.process_status),
		flowScope: stringValue(raw.flow_scope),
		deliveryGate: stringValue(raw.delivery_gate),
		deliveryReasons: strings(raw.delivery_gate_reasons),
		expectedFinalStep: stringValue(raw.expected_final_step),
		endpointReached: booleanValue(raw.endpoint_reached),
		mappedEquivalenceStatus: stringValue(mappedEquivalence.status),
		mappedEquivalenceGate: stringValue(mappedEquivalence.gate),
		mappedEquivalenceScope: stringValue(mappedEquivalence.scope),
		mappedEquivalenceReasons: strings(mappedEquivalence.reasons),
		openPdk: booleanValue(assurance.open_pdk),
		foundrySignoff: booleanValue(assurance.foundry_signoff),
		tapeoutGuarantee: booleanValue(assurance.tapeout_guarantee),
		assuranceLimitations: strings(assurance.limitations),
		visualizationStatus: stringValue(visualization.status),
		visualizationCount: finiteNumber(visualization.visualization_count) ?? 0,
		latestVisualization: stringValue(visualization.latest_image),
		visualizationErrors: strings(visualization.errors),
		stepHistory: stepRecords(raw.step_history),
		qorGate: stringValue(qor.gate),
		signoffGate: stringValue(qor.signoff_gate),
		physicalSanityGate: stringValue(physicalSanity.gate),
		qorReasons: strings(qor.gate_reasons),
	};
}

export function installSnapshot(
	kind: "pdk" | "image",
	raw: Record<string, unknown>,
): Snapshot | undefined {
	const runId = stringValue(raw.run_id);
	const status = stringValue(raw.status);
	if (!runId || !status) return undefined;
	return {
		kind,
		runId,
		status,
		phase: `${kind}_install`,
		completedSteps: booleanValue(raw.installed) === true ? 1 : 0,
		elapsedSeconds: finiteNumber(raw.elapsed_seconds) ?? 0,
		errors: strings(raw.errors),
	};
}
