import assert from "node:assert/strict";
import test from "node:test";

import {
	booleanValue,
	rtlClosureEvents,
	rtlClosureSnapshot,
	compactNotice,
	drainRtlClosureEvents,
	eventNoticeType,
	eventSummary,
	finiteNumber,
	fingerprint,
	heartbeatDue,
	heartbeatSummary,
	initialLaneEventCursor,
	initialWorkflowEventCursor,
	isManagedStateAbsent,
	jsonObjectArrayPayload,
	jsonObjectPayload,
	laneEventReport,
	laneTimelineEvents,
	laneSnapshot,
	monitorCheckpoint,
	monitorRunId,
	nextMonitorHealth,
	orchestratorEnvelope,
	orchestratorListEnvelope,
	record,
	sourceRunKey,
	statusType,
	strings,
	summary,
	widgetLines,
	workflowEventNoticeType,
	workflowEventReport,
	workflowEventSummary,
	workflowSnapshot,
} from "../extensions/lane-monitor-core.ts";

test("current direct CLI payloads reject generic response envelopes", () => {
	const current = { run_id: "current", status: "running" };
	assert.deepEqual(jsonObjectPayload(current), current);
	assert.equal(
		jsonObjectPayload({ schema_version: 2, ok: true, data: current }),
		undefined,
	);
	assert.equal(jsonObjectPayload({ result: current }), undefined);
	assert.equal(jsonObjectPayload({ ok: false, data: current }), undefined);
	assert.equal(jsonObjectPayload({ ok: "true", data: current }), undefined);
	assert.equal(jsonObjectPayload({ ok: true, data: [] }), undefined);
	assert.equal(jsonObjectPayload(null), undefined);
	assert.deepEqual(jsonObjectArrayPayload([current]), [current]);
	assert.equal(jsonObjectArrayPayload({ ok: true, data: [current] }), undefined);
	assert.deepEqual(jsonObjectArrayPayload([]), []);
	assert.equal(jsonObjectArrayPayload({ ok: false, data: [current] }), undefined);
	assert.equal(jsonObjectArrayPayload([current, null]), undefined);
});

test("orchestrator envelopes accept only current protocol 3 and exact kinds", () => {
	const data = { workflow_id: "workflow-1", status: "running" };
	assert.deepEqual(orchestratorEnvelope({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind: "run.get",
		request_id: "req-3",
		data,
	}, "run.get"), { value: data });
	assert.equal(orchestratorEnvelope({
		schema_version: 1,
		protocol_version: 2,
		ok: true,
		kind: "run.get",
		request_id: "req-2",
		data,
	}, "run.get"), undefined);
	assert.equal(orchestratorEnvelope({
		schema_version: 1,
		protocol_version: 1,
		ok: true,
		kind: "run.get",
		request_id: "req-1",
		data,
	}, "run.get"), undefined);
	assert.equal(orchestratorEnvelope({
		schema_version: 1,
		protocol_version: 4,
		ok: true,
		kind: "run.get",
		request_id: "req-4",
		data,
	}, "run.get"), undefined);
	assert.equal(orchestratorEnvelope({
		schema_version: 2,
		protocol_version: 3,
		ok: true,
		kind: "run.get",
		data,
	}, "run.get"), undefined);
	assert.equal(orchestratorEnvelope({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind: "run.events",
		data,
	}, "run.get"), undefined);
	assert.deepEqual(orchestratorEnvelope({
		schema_version: 1,
		protocol_version: 3,
		ok: false,
		kind: "error",
		request_id: null,
		error: {
			code: "RUN_NOT_FOUND",
			category: "state",
			message: "workflow does not exist",
			retryable: false,
		},
	}, "run.get"), {
		errorCode: "RUN_NOT_FOUND",
		errorMessage: "workflow does not exist",
		errorCategory: "state",
		retryable: false,
	});
	assert.deepEqual(orchestratorListEnvelope({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind: "run.list",
		request_id: "req-list",
		data: [data],
	}), { values: [data] });
	assert.equal(orchestratorListEnvelope({
		schema_version: 1,
		protocol_version: 2,
		ok: true,
		kind: "run.list",
		request_id: "req-list-v2",
		data: [data],
	}), undefined);
	assert.equal(orchestratorListEnvelope({
		schema_version: 1,
		protocol_version: 4,
		ok: true,
		kind: "run.list",
		request_id: "req-list-v4",
		data: [data],
	}), undefined);
	assert.equal(orchestratorListEnvelope({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind: "run.get",
		request_id: "req-list",
		data: [data],
	}), undefined);
});

test("workflow snapshots retain global lineage and structured failure ownership", () => {
	const snapshot = workflowSnapshot({
		workflow_id: "workflow-1",
		design: "fifo",
		pdk: "sky130A",
		status: "blocked_infrastructure",
		phase: "backend_status_unknown",
		revision: 7,
		rtl_closure_run_id: "rtl-1",
		lane_run_id: "lane-2",
		current_step: "formal",
		completed_step_count: 4,
		elapsed_seconds: 90,
		failure: {
			code: "BACKEND_TIMEOUT",
			domain: "infrastructure",
			message: "backend status timed out",
			retryable: true,
			repairable_by_rtl: false,
			user_action_required: false,
			evidence: ["/tmp/orchestrator.log"],
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.kind, "workflow");
	assert.equal(snapshot.workflowRevision, 7);
	assert.equal(snapshot.rtlClosureRunId, "rtl-1");
	assert.equal(snapshot.laneRunId, "lane-2");
	assert.equal(snapshot.failureRepairableByRtl, false);
	assert.equal(statusType(snapshot), "error");
	assert.match(summary(snapshot), /failure: BACKEND_TIMEOUT \(infrastructure\)/);
	assert.match(widgetLines(snapshot).join("\n"), /revision 7 \| RTL-Closure rtl-1 \| Lane lane-2/);
});

test("workflow event reports enforce sequence, hash chain, acknowledgement, and snapshot identity", () => {
	const firstHash = "a".repeat(64);
	const secondHash = "b".repeat(64);
	const snapshot = {
		workflow_id: "workflow-1",
		status: "running",
		phase: "lane_running",
		revision: 2,
		completed_step_count: 1,
		elapsed_seconds: 12,
	};
	const events = [
		{
			schema_version: 1,
			event_id: "workflow-1:1",
			sequence: 1,
			timestamp: "2026-07-28T12:00:00+08:00",
			source: "pi-lane-orchestrator",
			attempt_id: null,
			type: "workflow.created",
			severity: "info",
			state_revision: 0,
			phase: "created",
			payload: { status: "created" },
			failure: null,
			previous_sha256: null,
			sha256: firstHash,
		},
		{
			schema_version: 1,
			event_id: "workflow-1:2",
			sequence: 2,
			timestamp: "2026-07-28T12:00:01+08:00",
			source: "pi-lane-orchestrator",
			attempt_id: "lane-001-1234abcd",
			type: "lane.attempt_started",
			severity: "warning",
			state_revision: 2,
			phase: "lane_running",
			payload: { backend_run_id: "lane-1", ordinal: 1 },
			failure: null,
			previous_sha256: firstHash,
			sha256: secondHash,
		},
	];
	const report = workflowEventReport({
		workflow_id: "workflow-1",
		after_sequence: 0,
		next_after_sequence: 2,
		has_more: false,
		events,
		cursor_sha256: secondHash,
		acknowledged_sequence: 2,
		snapshot,
	});
	assert.ok(report);
	assert.equal(report.events.length, 2);
	assert.equal(workflowEventNoticeType(report.events[1]), "warning");
	assert.match(workflowEventSummary("workflow-1", report.events[1]), /backend run: lane-1/);
	assert.equal(workflowEventReport({
		workflow_id: "workflow-1",
		after_sequence: 0,
		next_after_sequence: 2,
		has_more: false,
		events: [events[1], events[0]],
		cursor_sha256: secondHash,
		acknowledged_sequence: 2,
		snapshot,
	}), undefined);
	const stable = workflowEventReport({
		workflow_id: "workflow-1",
		after_sequence: 2,
		next_after_sequence: 2,
		has_more: false,
		events: [],
		cursor_sha256: secondHash,
		acknowledged_sequence: 2,
		snapshot,
	});
	assert.ok(stable);
	assert.equal(stable.nextCursor.cursorSha256, secondHash);
});

test("source/run keys and compact notices remain bounded", () => {
	assert.equal(sourceRunKey("lane", "run-12"), "lane:run-12");
	assert.equal(monitorRunId("run-12"), "run-12");
	assert.equal(monitorRunId("run 12"), undefined);
	assert.equal(monitorRunId("x".repeat(201)), undefined);
	assert.equal(monitorRunId("-option-like"), undefined);
	const notice = compactNotice(Array.from({ length: 40 }, (_, index) => `line-${index}`).join("\n"), 320);
	assert.ok(notice.length <= 320);
	assert.match(notice, /omitted|truncated/);
});

test("current checkpoints keep claimed and most-recent cursors within the LRU bound", () => {
	const cursors = Object.fromEntries(Array.from({ length: 300 }, (_, index) => [
		`run-${String(index).padStart(3, "0")}`,
		{
			afterSequence: index,
			...(index > 0 ? { cursorSha256: "a".repeat(64) } : {}),
			suppressHistory: false,
		},
	]));
	const checkpoint = monitorCheckpoint({
		schemaVersion: 6,
		savedAt: 1_722_643_200_000,
		workflowAutoDiscovery: true,
		workflowClaim: "run-001",
		bindings: { workflow: "run-001" },
		pinnedSources: ["workflow"],
		recentTerminalWorkflow: "run-002",
		workflowEventCursors: cursors,
		rtlClosureEventCursors: {},
		laneEventCursors: {},
	});
	assert.ok(checkpoint);
	assert.equal(Object.keys(checkpoint.workflowEventCursors).length, 256);
	assert.ok(checkpoint.workflowEventCursors["run-001"]);
	assert.ok(checkpoint.workflowEventCursors["run-002"]);
	assert.ok(checkpoint.workflowEventCursors["run-299"]);
	assert.equal(checkpoint.recentTerminalWorkflow, "run-002");
	assert.equal(checkpoint.workflowClaim, "run-001");
	assert.equal(checkpoint.workflowEventCursors["run-003"], undefined);
});

test("current Lane event schema 3 exposes a strict resumable cursor", () => {
	const emptyHash = initialLaneEventCursor().cursorSha256;
	const requested = {
		afterSequence: 3,
		generation: "1a:2b",
		cursorSha256: "f".repeat(64),
		suppressHistory: false,
	};
	const report = laneEventReport({
		schema_version: 2,
		run_id: "lane-events",
		events: [
			{
				sequence: 4,
				event: "step_started",
				phase: "floorplan",
				status: "running",
				severity: "info",
				message: "floorplan started",
				ordinal: 2,
				step: "OpenROAD.Floorplan",
				directory: "/tmp/2-floorplan",
				logs: ["/tmp/2-floorplan/run.log"],
			},
		],
		after_sequence: 3,
		next_after_sequence: 4,
		has_more: false,
		invalid_line_count: 1,
		generation: "1a:2b",
		cursor_sha256: "a".repeat(64),
		snapshot: {
			status: "running",
			coarse_phase: "implementation",
			active_step: "OpenROAD.Floorplan",
			completed_step_count: 1,
			elapsed_seconds: 8,
			errors: [],
		},
	}, requested);
	assert.ok(report);
	assert.equal(report.runId, "lane-events");
	assert.equal(report.events.length, 1);
	assert.equal(report.events[0].logs[0], "/tmp/2-floorplan/run.log");
	assert.equal(report.events[0].phase, "floorplan");
	assert.equal(report.events[0].message, "floorplan started");
	assert.equal(report.snapshot.runId, "lane-events");
	assert.deepEqual(report.nextCursor, {
		afterSequence: 4,
		generation: "1a:2b",
		cursorSha256: "a".repeat(64),
		suppressHistory: false,
	});

	assert.equal(laneEventReport({
		run_id: "lane-events",
		events: [],
		after_sequence: 0,
		next_after_sequence: 1,
		has_more: false,
		invalid_line_count: 0,
		cursor_sha256: emptyHash,
		snapshot: { status: "running" },
	}), undefined, "a nonzero continuation requires a generation");
	assert.equal(laneEventReport({
		run_id: "lane-events",
		events: [{ sequence: 50, event: "step_started" }],
		after_sequence: 3,
		next_after_sequence: 50,
		has_more: false,
		invalid_line_count: 0,
		generation: "1a:2b",
		cursor_sha256: "a".repeat(64),
		snapshot: { status: "running" },
	}, requested), undefined, "a response cannot skip the requested event cursor");
	assert.deepEqual(laneTimelineEvents([null, [], { sequence: 1, event: "ok" }]).map((item) => item.event), ["ok"]);
});

test("event cursors distinguish restored history from live attachment", () => {
	assert.equal(initialWorkflowEventCursor().suppressHistory, true);
	assert.equal(initialWorkflowEventCursor(false).suppressHistory, false);
	assert.equal(initialLaneEventCursor().suppressHistory, true);
	assert.equal(initialLaneEventCursor(false).suppressHistory, false);
});

test("monitor checkpoints restore only validated per-run cursors", () => {
	const checkpoint = monitorCheckpoint({
		schemaVersion: 6,
		workflowAutoDiscovery: false,
		workflowClaim: "workflow-1",
		bindings: { rtl_closure: "rtl-1", lane: "lane-1", pdk: "bad id" },
		pinnedSources: ["rtl_closure"],
		workflowEventCursors: {
			"workflow-1": {
				afterSequence: 9,
				cursorSha256: "c".repeat(64),
				suppressHistory: false,
			},
		},
		rtlClosureEventCursors: { "rtl-1": 12, bad: -1 },
		laneEventCursors: {
			"lane-1": {
				afterSequence: 8,
				generation: "a:b",
				cursorSha256: "f".repeat(64),
				suppressHistory: false,
			},
			"lane-bad": {
				afterSequence: 9,
				cursorSha256: "f".repeat(64),
				suppressHistory: false,
			},
		},
	});
	assert.ok(checkpoint);
	assert.equal(checkpoint.schemaVersion, 6);
	assert.equal(checkpoint.workflowAutoDiscovery, false);
	assert.equal(checkpoint.workflowClaim, "workflow-1");
	assert.deepEqual(checkpoint.bindings, { rtl_closure: "rtl-1", lane: "lane-1" });
	assert.deepEqual(checkpoint.pinnedSources, ["rtl_closure"]);
	assert.equal(checkpoint.workflowEventCursors["workflow-1"].afterSequence, 9);
	assert.deepEqual(checkpoint.rtlClosureEventCursors, { "rtl-1": 12 });
	assert.deepEqual(Object.keys(checkpoint.laneEventCursors), ["lane-1"]);
	assert.equal(monitorCheckpoint({ schemaVersion: 5 }), undefined);
	assert.equal(monitorCheckpoint({
		schemaVersion: 6,
		workflowAutoDiscovery: true,
		bindings: {},
		pinnedSources: [],
		workflowEventCursors: {},
		rtlClosureEventCursors: {},
		laneEventCursors: {},
		recentTerminalWorkflow: "bad workflow id",
	}), undefined);
});

test("checkpoint parser rejects all predecessor schemas and aliases", () => {
	assert.equal(monitorCheckpoint({ schemaVersion: 5 }), undefined);
	assert.equal(monitorCheckpoint({
		schema_version: 6,
		workflow_auto_discovery: true,
		bindings: {},
		pinned_sources: [],
		workflow_event_cursors: {},
		rtl_closure_event_cursors: {},
		lane_event_cursors: {},
	}), undefined);
	assert.equal(monitorCheckpoint({
		schemaVersion: 6,
		workflowAutoDiscovery: true,
		bindings: {},
		pinnedSources: ["retired_rtl_source"],
		workflowEventCursors: {},
		rtlClosureEventCursors: {},
		laneEventCursors: {},
	}), undefined);
});

test("lane QoR failures remain terminal errors with visible sanity detail", () => {
	const snapshot = laneSnapshot({
		run_id: "lane-qor-failure",
		status: "failed",
		coarse_phase: "qor_gate",
		completed_step_count: 72,
		elapsed_seconds: 95,
		errors: ["[QoR ERROR] physical_sanity.minimum_utilization failed"],
		qor: {
			gate: "fail",
			signoff_gate: "pass",
			physical_sanity: { gate: "fail" },
			gate_reasons: [
				"physical_sanity.minimum_utilization: standard-cell utilization 5.2% is too low",
			],
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.qorGate, "fail");
	assert.equal(snapshot.signoffGate, "pass");
	assert.equal(snapshot.physicalSanityGate, "fail");
	assert.equal(statusType(snapshot), "error");
	const rendered = summary(snapshot);
	assert.match(
		rendered,
		/QoR gate: fail \(LibreLane signoff-check gate pass, physical sanity fail\)/,
	);
	assert.match(rendered, /QoR failure: physical_sanity\.minimum_utilization/);
});

test("delivery scope and incomplete gate are visible as a warning", () => {
	const snapshot = laneSnapshot({
		run_id: "lane-delivery-incomplete",
		status: "incomplete",
		coarse_phase: "delivery_gate",
		process_status: "completed",
		flow_scope: "full",
		delivery_gate: "incomplete",
		delivery_gate_reasons: ["final QoR gate is unknown, not pass"],
		expected_final_step: "Misc.ReportManufacturability",
		endpoint_reached: true,
		completed_step_count: 76,
		elapsed_seconds: 95,
		errors: [],
		qor: { gate: "unknown" },
	});
	assert.ok(snapshot);
	assert.equal(snapshot.processStatus, "completed");
	assert.equal(snapshot.flowScope, "full");
	assert.equal(snapshot.deliveryGate, "incomplete");
	assert.equal(snapshot.endpointReached, true);
	assert.equal(statusType(snapshot), "warning");
	assert.match(summary(snapshot), /delivery: incomplete \(scope full\)/);
	assert.match(summary(snapshot), /delivery reason: final QoR gate is unknown/);
	assert.match(widgetLines(snapshot).join("\n"), /process completed \| scope full \| delivery incomplete/);
});

test("lane assurance exposes mapped-equivalence scope and open-PDK boundaries", () => {
	const snapshot = laneSnapshot({
		run_id: "lane-assurance",
		status: "completed",
		coarse_phase: "completed",
		completed_step_count: 80,
		elapsed_seconds: 123,
		errors: [],
		mapped_equivalence: {
			status: "pass",
			gate: "pass",
			scope: "RTL versus synthesized PDK-mapped netlist equivalence only; this is not post-layout LEC",
			reasons: [],
		},
		assurance: {
			open_pdk: true,
			foundry_signoff: false,
			tapeout_guarantee: false,
			limitations: [
				"Open-source PDK checks are not commercial foundry signoff.",
				"Passing automated checks does not guarantee tapeout acceptance.",
			],
		},
		qor: {
			gate: "pass",
			signoff_gate: "pass",
			physical_sanity: { gate: "pass" },
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.mappedEquivalenceStatus, "pass");
	assert.equal(snapshot.mappedEquivalenceGate, "pass");
	assert.match(snapshot.mappedEquivalenceScope ?? "", /not post-layout LEC/);
	assert.deepEqual(snapshot.mappedEquivalenceReasons, []);
	assert.equal(snapshot.openPdk, true);
	assert.equal(snapshot.foundrySignoff, false);
	assert.equal(snapshot.tapeoutGuarantee, false);
	assert.equal(snapshot.assuranceLimitations?.length, 2);

	const rendered = summary(snapshot);
	assert.match(rendered, /mapped equivalence: pass \(gate pass\)/);
	assert.match(rendered, /mapped equivalence scope: .*not post-layout LEC/);
	assert.match(
		rendered,
		/assurance: open PDK true, foundry signoff false, tapeout guarantee false/,
	);
	assert.match(rendered, /assurance limitation: Open-source PDK checks/);
	assert.match(rendered, /LibreLane signoff-check gate pass/);
	assert.doesNotMatch(rendered, /foundry signoff (?:true|pass)/);

	const widget = widgetLines(snapshot).join("\n");
	assert.match(widget, /mapped equivalence pass \| gate pass/);
	assert.match(
		widget,
		/assurance open PDK true \| foundry signoff false \| tapeout guarantee false/,
	);
	assert.match(widget, /QoR pass \| LibreLane checks pass \| sanity pass/);
	assert.doesNotMatch(widget, /foundry signoff (?:true|pass)/);

	assert.notEqual(
		fingerprint(snapshot),
		fingerprint({ ...snapshot, mappedEquivalenceStatus: "incomplete" }),
	);
	assert.notEqual(
		fingerprint(snapshot),
		fingerprint({ ...snapshot, foundrySignoff: true }),
	);
});

test("lane assurance parsing does not coerce string booleans", () => {
	const snapshot = laneSnapshot({
		run_id: "lane-assurance-invalid-booleans",
		status: "running",
		assurance: {
			open_pdk: "true",
			foundry_signoff: "false",
			tapeout_guarantee: 0,
			limitations: [null, "Known boundary"],
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.openPdk, undefined);
	assert.equal(snapshot.foundrySignoff, undefined);
	assert.equal(snapshot.tapeoutGuarantee, undefined);
	assert.deepEqual(snapshot.assuranceLimitations, ["Known boundary"]);
	assert.doesNotMatch(summary(snapshot), /assurance: open PDK/);
	assert.match(summary(snapshot), /assurance limitation: Known boundary/);
});

test("strict JSON helpers reject null, arrays, and coerced scalar types", () => {
	assert.equal(record(null), undefined);
	assert.equal(record([]), undefined);
	assert.deepEqual(record({ value: 1 }), { value: 1 });
	assert.equal(finiteNumber(null), undefined);
	assert.equal(finiteNumber("4"), undefined);
	assert.equal(finiteNumber(Number.POSITIVE_INFINITY), undefined);
	assert.equal(finiteNumber(4), 4);
	assert.equal(booleanValue("false"), undefined);
	assert.equal(booleanValue(false), false);
	assert.deepEqual(strings(["one", null, 2, {}, "two"]), ["one", "two"]);
});

test("poll health distinguishes absent state and reports degrade/recovery once", () => {
	assert.equal(isManagedStateAbsent("rtl-closure: error: no RTL-Closure run exists"), true);
	assert.equal(isManagedStateAbsent("lane-agent: error: no managed LibreLane run exists"), true);
	assert.equal(isManagedStateAbsent("lane-agent: error: no managed PDK installation exists"), true);
	assert.equal(isManagedStateAbsent("rtl-closure: error: run does not exist: rtl-1"), true);
	assert.equal(isManagedStateAbsent("lane-agent: error: PDK installation does not exist: pdk-1"), true);
	assert.equal(isManagedStateAbsent("lane-agent: error: image installation does not exist: image-1"), true);
	assert.equal(isManagedStateAbsent("connection refused"), false);

	let health = {
		degraded: false,
		consecutiveFailures: 0,
		transition: "unchanged" as const,
	};
	health = nextMonitorHealth(health, ["rtl-closure report: timeout"]);
	assert.equal(health.degraded, true);
	assert.equal(health.transition, "degraded");
	assert.equal(health.consecutiveFailures, 1);
	health = nextMonitorHealth(health, ["rtl-closure report: timeout"]);
	assert.equal(health.transition, "unchanged");
	assert.equal(health.consecutiveFailures, 2);
	health = nextMonitorHealth(health, []);
	assert.equal(health.degraded, false);
	assert.equal(health.transition, "recovered");
	health = nextMonitorHealth(health, []);
	assert.equal(health.transition, "unchanged");
});

test("running heartbeats are rate limited and include the current snapshot", () => {
	assert.equal(heartbeatDue("running", undefined, 60_000, 60_000), false);
	assert.equal(heartbeatDue("running", 1_000, 60_999, 60_000), false);
	assert.equal(heartbeatDue("running", 1_000, 61_000, 60_000), true);
	assert.equal(heartbeatDue("completed", 1_000, 61_000, 60_000), false);
	const snapshot = rtlClosureSnapshot({
		run_id: "run-heartbeat",
		status: "running",
		phase: "generating",
		elapsed_seconds: 120,
	});
	assert.ok(snapshot);
	const rendered = heartbeatSummary(snapshot);
	assert.match(rendered, /^\[Pi Lane\] RTL-Closure heartbeat/m);
	assert.match(rendered, /phase: generating/);
	assert.match(rendered, /elapsed: 120s/);
});

test("model request progress is visible without exposing model content or reasoning", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-model-stream",
		status: "running",
		phase: "generating",
		elapsed_seconds: 78,
		model_request: {
			event: "streaming",
			request_id: 17,
			candidate_index: 2,
			candidate_count: 4,
			attempt: 1,
			active_request_count: 2,
			elapsed_seconds: 45,
			chunk_count: 9,
			prompt: "PRIVATE PROMPT MUST NOT APPEAR",
			response: "PRIVATE RTL MUST NOT APPEAR",
			reasoning: "PRIVATE REASONING MUST NOT APPEAR",
			content_chars: 1234,
			active_requests: [{
				request_id: 17,
				max_attempts: 3,
				first_event_seconds: 1.25,
				reasoning: "NESTED PRIVATE REASONING",
			}],
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.modelRequest?.event, "streaming");
	assert.equal(snapshot.modelRequest?.candidate, 2);
	assert.equal(snapshot.modelRequest?.candidateCount, 4);
	assert.equal(snapshot.modelRequest?.attempt, 1);
	assert.equal(snapshot.modelRequest?.maxAttempts, 3);
	assert.equal(snapshot.modelRequest?.activeRequestCount, 2);
	assert.equal(snapshot.modelRequest?.elapsedSeconds, 45);
	assert.equal(snapshot.modelRequest?.firstEventSeconds, 1.25);
	assert.equal(snapshot.modelRequest?.chunkCount, 9);

	const rendered = [summary(snapshot), heartbeatSummary(snapshot), ...widgetLines(snapshot)]
		.join("\n");
	assert.match(
		rendered,
		/model request: stream heartbeat, request 17, candidate 2\/4, attempt 1\/3, active 2/,
	);
	assert.match(rendered, /model stream: elapsed 45s, first event 1.25s, chunks 9/);
	assert.doesNotMatch(rendered, /PRIVATE|REASONING|content_chars|1234/);
	assert.equal(statusType(snapshot), "info");
	assert.notEqual(
		fingerprint(snapshot),
		fingerprint({
			...snapshot,
			modelRequest: { ...snapshot.modelRequest!, elapsedSeconds: 46 },
		}),
	);
});

test("model request waiting heartbeat is visible before the first SSE event", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-model-waiting",
		status: "running",
		phase: "generating",
		model_request: {
			event: "request_waiting",
			request_id: 18,
			candidate_index: 1,
			candidate_count: 2,
			attempt: 1,
			max_attempts: 3,
			active_request_count: 2,
			elapsed_seconds: 30,
			chunk_count: 0,
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.modelRequest?.event, "request_waiting");
	assert.match(summary(snapshot), /model request: waiting for stream/);
	assert.match(summary(snapshot), /elapsed 30s, chunks 0/);
});

test("model request lifecycle events report connection, retry, completion, and failure", () => {
	const events = rtlClosureEvents([
		{
			sequence: 30,
			status: "running",
			phase: "generating",
			model_request: {
				event: "request_started",
				request_id: 8,
				candidate_index: 1,
				candidate_count: 4,
				attempt: 1,
				max_attempts: 3,
				active_request_count: 1,
			},
		},
		{
			sequence: 31,
			status: "running",
			phase: "generating",
			model_request: {
				event: "stream_connected",
				request_id: 8,
				candidate_index: 1,
				candidate_count: 4,
				attempt: 1,
				first_event_seconds: 2.5,
			},
		},
		{
			sequence: 32,
			status: "running",
			phase: "generating",
			model_request: {
				event: "streaming",
				request_id: 8,
				candidate_index: 1,
				candidate_count: 4,
				attempt: 1,
				elapsed_seconds: 30,
				chunk_count: 7,
			},
		},
		{
			sequence: 33,
			status: "running",
			phase: "generating",
			model_request: {
				event: "request_completed",
				request_id: 8,
				candidate_index: 1,
				candidate_count: 4,
				attempt: 1,
				elapsed_seconds: 83,
				chunk_count: 21,
				response: "DO NOT DISPLAY THIS RESPONSE",
			},
		},
		{
			sequence: 34,
			status: "running",
			phase: "generating",
			model_request: {
				event: "request_retrying",
				request_id: 9,
				candidate_index: 2,
				candidate_count: 4,
				attempt: 1,
				max_attempts: 3,
				retry_in_seconds: 120,
				retry_after_seconds: 120,
				http_status: 524,
				error_type: "InternalServerError",
				ray_id: "a1f9611da80f2656",
			},
		},
		{
			sequence: 35,
			status: "running",
			phase: "generating",
			model_request: {
				event: "request_failed",
				request_id: 9,
				candidate_index: 2,
				candidate_count: 4,
				attempt: 3,
				max_attempts: 3,
				http_status: 524,
				retryable: true,
				reasoning: "DO NOT DISPLAY THIS REASONING",
			},
		},
	]);
	assert.equal(events.length, 6);
	assert.match(eventSummary("run-model", events[0]), /model request: started/);
	assert.match(eventSummary("run-model", events[1]), /model request: stream connected/);
	assert.match(eventSummary("run-model", events[1]), /first event 2.5s/);
	assert.match(eventSummary("run-model", events[2]), /model request: stream heartbeat/);
	assert.match(eventSummary("run-model", events[2]), /elapsed 30s, chunks 7/);
	assert.match(eventSummary("run-model", events[3]), /model request: completed/);
	assert.match(eventSummary("run-model", events[3]), /elapsed 83s, chunks 21/);
	const retry = eventSummary("run-model", events[4]);
	assert.match(retry, /model request: retrying/);
	assert.match(retry, /model retry: in 120s, provider delay 120s/);
	assert.match(retry, /model error: InternalServerError, HTTP 524, ray a1f9611da80f2656/);
	assert.equal(eventNoticeType(events[4]), "warning");
	const failed = eventSummary("run-model", events[5]);
	assert.match(failed, /model request: failed/);
	assert.match(failed, /HTTP 524, retryable true/);
	assert.doesNotMatch(`${retry}\n${failed}\n${eventSummary("run-model", events[3])}`, /DO NOT DISPLAY/);
	assert.equal(eventNoticeType(events[5]), "warning");
	assert.equal(eventNoticeType({ ...events[5], status: "failed" }), "error");
});

test("rtl_closure snapshots parse repair progress without truthiness coercion", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-1",
		status: "running",
		phase: "repair_evaluating",
		elapsed_seconds: null,
		errors: [null, 3, "real error"],
		repair: {
			round: 2,
			max_rounds: 6,
			candidate: 1,
			candidate_count: 3,
			failure_class: "functional_mismatch",
			requirement_id: "REQ_WRAP",
			first_failure_cycle: null,
			strategy: "local",
			result: { passed: false, failure_class: "timing_shift" },
			eligible: false,
			reason: "does not improve the champion",
			score: [1, 20, -3],
			stagnation: 0,
			action: "evaluate",
			counterexample_count: 2,
		},
		verification: {
			passed: "false",
			total_checks: null,
			mismatched: [],
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.elapsedSeconds, 0);
	assert.deepEqual(snapshot.errors, ["real error"]);
	assert.equal(snapshot.repairRound, 2);
	assert.equal(snapshot.repairRequirementId, "REQ_WRAP");
	assert.equal(snapshot.repairFirstFailureCycle, undefined);
	assert.equal(snapshot.repairStrategy, "local");
	assert.equal(snapshot.repairCandidateResult, "failed (timing_shift)");
	assert.equal(snapshot.repairCandidateEligible, false);
	assert.equal(snapshot.repairCandidateReason, "does not improve the champion");
	assert.deepEqual(snapshot.repairCandidateScore, [1, 20, -3]);
	assert.equal(snapshot.repairStagnation, 0);
	assert.equal(snapshot.repairAction, "evaluate");
	assert.equal(snapshot.verificationPassed, undefined);
	assert.equal(snapshot.verificationChecks, undefined);
});

test("accepted and successful snapshots clear stale failure attribution", () => {
	const accepted = rtlClosureSnapshot({
		run_id: "run-2",
		status: "running",
		phase: "repair_accepted",
		repair: {
			passed: true,
			failure_class: "functional_mismatch",
			requirement_id: "REQ_OLD",
			first_failure_cycle: 4,
			action: "accept",
		},
	});
	assert.ok(accepted);
	assert.equal(accepted.repairFailureClass, undefined);
	assert.equal(accepted.repairRequirementId, undefined);
	assert.equal(accepted.repairFirstFailureCycle, undefined);
	assert.equal(accepted.repairAction, "accept");

	const passed = rtlClosureSnapshot({
		run_id: "run-2",
		status: "running",
		phase: "verification_complete",
		repair: {
			failure_class: "functional_mismatch",
			requirement_id: "REQ_OLD",
			first_failure_cycle: 4,
		},
		verification: { passed: true, total_checks: 10, mismatched: 0 },
	});
	assert.ok(passed);
	assert.equal(passed.repairFailureClass, undefined);
	assert.equal(passed.repairRequirementId, undefined);
	assert.equal(passed.repairFirstFailureCycle, undefined);
	assert.equal(passed.verificationPassed, true);

	const acceptedEvent = rtlClosureEvents([{
		sequence: 9,
		status: "running",
		phase: "repair_accepted",
		repair: {
			passed: true,
			failure_class: "functional_mismatch",
			requirement_id: "REQ_OLD",
			first_failure_cycle: 4,
			action: "accept",
		},
	}])[0];
	const text = eventSummary("run-2", acceptedEvent);
	assert.doesNotMatch(text, /failure class|first failure cycle|requirement:/);
	assert.match(text, /action: accept/);

	const partial = rtlClosureSnapshot({
		run_id: "run-2",
		status: "running",
		phase: "repair_accepted",
		repair: {
			passed: false,
			failure_class: "value_mismatch",
			requirement_id: "REQ_CURRENT",
			first_failure_cycle: 8,
			action: "continue",
		},
	});
	assert.ok(partial);
	assert.equal(partial.repairFailureClass, "value_mismatch");
	assert.equal(partial.repairRequirementId, "REQ_CURRENT");
	assert.equal(partial.repairFirstFailureCycle, 8);
});

test("repair context participates in summaries and fingerprints", () => {
	const first = rtlClosureSnapshot({
		run_id: "run-3",
		status: "running",
		phase: "repair_candidate_completed",
		repair: {
			round: 1,
			candidate: 2,
			requirement_id: "REQ_PRIORITY",
			strategy: "local",
			candidate_result: "failed",
			candidate_reason: "new mismatch",
			candidate_score: 12,
			stagnation: 1,
			action: "rollback",
		},
	});
	assert.ok(first);
	const rendered = summary(first);
	assert.match(rendered, /requirement: REQ_PRIORITY/);
	assert.match(rendered, /strategy: local/);
	assert.match(rendered, /candidate result: failed/);
	assert.match(rendered, /candidate reason: new mismatch/);
	assert.match(rendered, /candidate score: 12/);
	assert.match(rendered, /stagnation: 1/);
	assert.match(rendered, /action: rollback/);

	const second = { ...first, repairAction: "accept" };
	assert.notEqual(fingerprint(first), fingerprint(second));
});

test("coverage, adaptive suite, and formal progress are visible", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-closure",
		status: "running",
		phase: "formal_completed",
		coverage: {
			round: 2,
			max_rounds: 3,
			coverage_percent: 91.5,
			coverage_target_pct: 95,
			remaining_cycle_budget: 12,
			status: "warning",
			target_met: false,
			warning: "oracle executable-line coverage target was not met",
		},
		repair: {
			failure_class: "old_failure",
			added_scenario_count: 4,
			epoch: 2,
			suite_sha256: "abc123",
			formal_mode: "required",
			formal_status: "pass",
			verilator_status: "pass",
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.coveragePercent, 91.5);
	assert.equal(snapshot.coverageRemainingCycles, 12);
	assert.equal(snapshot.coverageStatus, "warning");
	assert.equal(snapshot.coverageTargetMet, false);
	assert.equal(snapshot.adaptiveAddedScenarios, 4);
	assert.equal(snapshot.formalStatus, "pass");
	assert.equal(snapshot.repairFailureClass, undefined);
	const rendered = summary(snapshot);
	assert.match(rendered, /oracle coverage: 91.5% \/ 95%/);
	assert.match(rendered, /remaining budget 12 cycles/);
	assert.match(rendered, /coverage status: warning, target met: false/);
	assert.match(rendered, /coverage warning: oracle executable-line coverage target was not met/);
	assert.match(rendered, /adaptive scenarios: \+4/);
	assert.match(rendered, /suite epoch: 2/);
	assert.match(rendered, /formal status: pass/);

	const event = rtlClosureEvents([{
		sequence: 1,
		status: "running",
		phase: "coverage_refining",
		coverage: {
			round: 1,
			max_rounds: 2,
			coverage_percent: 72,
			coverage_target_pct: 85,
			remaining_cycle_budget: 6,
		},
	}])[0];
	const eventText = eventSummary("run-closure", event);
	assert.match(eventText, /oracle coverage: 72% \/ 85%/);
	assert.match(eventText, /remaining budget 6 cycles/);

	const failedRefinement = rtlClosureEvents([{
		sequence: 2,
		status: "running",
		phase: "coverage_refinement_error",
		coverage: {
			status: "error",
			target_met: false,
			coverage_percent: 72,
			coverage_target_pct: 85,
			remaining_cycle_budget: 6,
			warning: "oracle coverage target was not met",
			error: "planner returned invalid JSON",
		},
	}])[0];
	assert.equal(eventNoticeType(failedRefinement), "error");
	assert.match(
		eventSummary("run-closure", failedRefinement),
		/coverage error: planner returned invalid JSON/,
	);

	const persistedFallback = rtlClosureSnapshot({
		run_id: "run-closure",
		status: "running",
		phase: "oracle_quorum_revalidating",
		coverage: failedRefinement.coverage,
	});
	assert.ok(persistedFallback);
	assert.equal(statusType(persistedFallback), "warning");
	assert.equal(statusType(snapshot), "warning");
});

test("architecture and synthesis-equivalence release evidence is visible", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-release",
		status: "running",
		phase: "release_synthesis_equivalence",
		architecture_audit: {
			status: "pass",
			failed_checks: [],
			artifact: "/tmp/run/architecture-audit.json",
		},
		synthesis_equivalence: {
			status: "pass",
			equivalence_cells: 58,
			proven_cells: 58,
			artifact: "/tmp/run/synthesis-equivalence.json",
			log: "/tmp/run/synthesis-equivalence.log",
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.architectureAuditStatus, "pass");
	assert.equal(snapshot.synthesisEquivalenceProvenCells, 58);
	const rendered = summary(snapshot);
	assert.match(rendered, /architecture audit: pass/);
	assert.match(rendered, /synthesis equivalence: pass, proven 58\/58/);
	assert.match(rendered, /synthesis-equivalence\.log/);

	const event = rtlClosureEvents([{
		sequence: 12,
		status: "running",
		phase: "release_architecture_audit",
		architecture_audit: {
			status: "fail",
			failed_checks: ["required_module:control_unit"],
			artifact: "/tmp/run/architecture-audit.json",
		},
	}])[0];
	assert.equal(eventNoticeType(event), "error");
	assert.match(
		eventSummary("run-release", event),
		/architecture audit: fail, failed: required_module:control_unit/,
	);
	const changed = { ...snapshot, synthesisEquivalenceProvenCells: 57 };
	assert.notEqual(fingerprint(snapshot), fingerprint(changed));
});

test("required formal infrastructure failures are errors with actionable detail", () => {
	const required = rtlClosureEvents([{
		sequence: 7,
		status: "running",
		phase: "formal_completed",
		repair: {
			formal_mode: "required",
			formal_status: "error",
			formal_detail: "yosys exited before proving any property",
			formal_artifact: "/tmp/run/formal-report.json",
		},
	}])[0];
	assert.equal(eventNoticeType(required), "error");
	const text = eventSummary("run-formal", required);
	assert.match(text, /formal detail: yosys exited before proving any property/);
	assert.match(text, /formal artifact: \/tmp\/run\/formal-report.json/);

	const advisory = rtlClosureEvents([{
		sequence: 8,
		status: "running",
		phase: "formal_completed",
		repair: { formal_mode: "advisory", formal_status: "unknown" },
	}])[0];
	assert.equal(eventNoticeType(advisory), "warning");

	const lintFailure = rtlClosureEvents([{
		sequence: 9,
		status: "running",
		phase: "formal_completed",
		repair: {
			formal_mode: "off",
			formal_status: "not_run",
			verilator_status: "fail",
		},
	}])[0];
	assert.equal(eventNoticeType(lintFailure), "warning");

	const mixedFailure = rtlClosureEvents([{
		sequence: 10,
		status: "running",
		phase: "formal_completed",
		repair: {
			formal_mode: "required",
			formal_status: "fail",
			verilator_status: "unknown",
		},
	}])[0];
	assert.equal(eventNoticeType(mixedFailure), "error");

	const snapshot = rtlClosureSnapshot({
		run_id: "run-formal",
		status: "running",
		phase: "regression",
		repair: { formal_mode: "required", formal_status: "fail" },
		verification: {
			formal_mode: "required",
			formal: {
				status: "error",
				detail: "solver unavailable",
				artifact: "/tmp/run/final-formal.json",
			},
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.formalStatus, "error");
	assert.equal(snapshot.formalDetail, "solver unavailable");
	assert.equal(statusType(snapshot), "error");

	const lintSnapshot = rtlClosureSnapshot({
		run_id: "run-lint",
		status: "running",
		phase: "formal_completed",
		repair: {
			formal_mode: "off",
			formal_status: "not_run",
			verilator_status: "fail",
		},
	});
	assert.ok(lintSnapshot);
	assert.equal(statusType(lintSnapshot), "warning");
});

test("universal property evidence is visible without treating unknown as pass", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-universal",
		status: "running",
		phase: "regression",
		verification: {
			universal_mode: "required",
			universal_properties: {
				status: "unknown",
				detail: "temporal induction timed out",
				artifact: "/tmp/run/universal-properties.json",
				property_count: 3,
				passed_property_ids: ["PROP_RESET"],
				failed_property_ids: [],
				universal_proof: false,
			},
		},
	});
	assert.ok(snapshot);
	assert.equal(snapshot.universalMode, "required");
	assert.equal(snapshot.universalStatus, "unknown");
	assert.equal(snapshot.universalPropertyCount, 3);
	assert.equal(snapshot.universalPassedCount, 1);
	assert.deepEqual(snapshot.universalFailedPropertyIds, []);
	assert.equal(snapshot.universalProof, false);
	assert.equal(statusType(snapshot), "error");
	const rendered = summary(snapshot);
	assert.match(rendered, /universal mode: required/);
	assert.match(rendered, /universal status: unknown/);
	assert.match(rendered, /universal properties: 1\/3 passed/);
	assert.match(rendered, /universal proof: false/);
	assert.match(rendered, /universal detail: temporal induction timed out/);
	assert.match(rendered, /universal artifact: \/tmp\/run\/universal-properties\.json/);
	assert.match(widgetLines(snapshot).join("\n"), /universal unknown \| mode required \| passed 1\/3 \| proof false/);
	assert.notEqual(
		fingerprint(snapshot),
		fingerprint({ ...snapshot, universalPassedCount: 2 }),
	);

	const counterexample = rtlClosureEvents([{
		sequence: 20,
		status: "running",
		phase: "universal_formal_completed",
		repair: {
			universal_mode: "required",
			universal_status: "fail",
			universal_detail: "PROP_ACCEPT has a DUT counterexample",
			universal_artifact: "/tmp/attempt/universal-properties.json",
			property_count: 3,
			passed_count: 2,
			failed_property_ids: ["PROP_ACCEPT"],
			universal_proof: false,
		},
	}])[0];
	assert.equal(counterexample.universalStatus, "fail");
	assert.equal(counterexample.universalPassedCount, 2);
	assert.deepEqual(counterexample.universalFailedPropertyIds, ["PROP_ACCEPT"]);
	assert.equal(eventNoticeType(counterexample), "warning");
	const eventText = eventSummary("run-universal", counterexample);
	assert.match(eventText, /universal status: fail/);
	assert.match(eventText, /universal failed properties: PROP_ACCEPT/);
	assert.match(eventText, /universal artifact: \/tmp\/attempt\/universal-properties\.json/);

	const requiredUnknown = rtlClosureEvents([{
		sequence: 21,
		status: "running",
		phase: "universal_formal_completed",
		repair: { universal_mode: "required", universal_status: "unknown" },
	}])[0];
	assert.equal(eventNoticeType(requiredUnknown), "error");

	const advisoryError = rtlClosureEvents([{
		sequence: 22,
		status: "running",
		phase: "universal_formal_completed",
		repair: { universal_mode: "advisory", universal_status: "error" },
	}])[0];
	assert.equal(eventNoticeType(advisoryError), "warning");
});

test("candidate architecture audit progress includes failed checks and evidence", () => {
	const snapshot = rtlClosureSnapshot({
		run_id: "run-candidate-architecture",
		status: "running",
		phase: "architecture_audit_completed",
		repair: {
			result: "failed",
			failed_checks: ["required_module:control_unit", "minimum_instances:serializer"],
			architecture_artifact: "/tmp/attempt/architecture-audit.json",
		},
	});
	assert.ok(snapshot);
	assert.deepEqual(snapshot.repairArchitectureFailedChecks, [
		"required_module:control_unit",
		"minimum_instances:serializer",
	]);
	assert.equal(
		snapshot.repairArchitectureArtifact,
		"/tmp/attempt/architecture-audit.json",
	);
	assert.match(
		summary(snapshot),
		/candidate architecture failed checks: required_module:control_unit, minimum_instances:serializer/,
	);
	assert.match(
		widgetLines(snapshot).join("\n"),
		/candidate architecture evidence \/tmp\/attempt\/architecture-audit\.json/,
	);
	assert.notEqual(
		fingerprint(snapshot),
		fingerprint({ ...snapshot, repairArchitectureFailedChecks: [] }),
	);

	const event = rtlClosureEvents([{
		sequence: 23,
		status: "running",
		phase: "architecture_audit_completed",
		repair: {
			failed_checks: ["required_module:control_unit"],
			architecture_artifact: "/tmp/attempt/architecture-audit.json",
		},
	}])[0];
	assert.match(
		eventSummary("run-candidate-architecture", event),
		/candidate architecture evidence: \/tmp\/attempt\/architecture-audit\.json/,
	);
});

test("event parser rejects malformed records and nested arrays", () => {
	const events = rtlClosureEvents([
		null,
		[],
		{ sequence: "1", status: "running", phase: "contracting" },
		{ sequence: 2, status: "running", phase: "repair", repair: [] },
		{ sequence: 2, status: "running", phase: "repair_latest" },
		{ sequence: 3, status: "running", phase: "verify", verification: [] },
	]);
	assert.deepEqual(events.map((event) => event.sequence), [2, 3]);
	assert.equal(events[0].phase, "repair_latest");
	assert.equal(events[0].repair, undefined);
	assert.equal(events[1].verification, undefined);
});

test("event drain suppresses initial history and reports later gaps", () => {
	const history = rtlClosureEvents(Array.from({ length: 256 }, (_, index) => ({
		sequence: index + 1,
		status: "running",
		phase: `phase_${index + 1}`,
	})));
	const attached = drainRtlClosureEvents(history, undefined);
	assert.equal(attached.initialAttach, true);
	assert.equal(attached.cursor, 256);
	assert.deepEqual(attached.pending, []);

	const incremental = drainRtlClosureEvents(rtlClosureEvents([
		{ sequence: 256, status: "running", phase: "old" },
		{ sequence: 257, status: "running", phase: "next" },
	]), attached.cursor);
	assert.deepEqual(incremental.pending.map((event) => event.sequence), [257]);
	assert.deepEqual(incremental.gaps, []);

	const truncated = drainRtlClosureEvents(rtlClosureEvents([
		{ sequence: 261, status: "running", phase: "available" },
		{ sequence: 262, status: "running", phase: "newest" },
	]), incremental.cursor);
	assert.deepEqual(truncated.gaps, [{ expected: 258, actual: 261 }]);
	assert.deepEqual(truncated.pending.map((event) => event.sequence), [261, 262]);
	assert.equal(truncated.cursor, 262);
});
