import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

const tuiModule = encodeURIComponent(`
export class Box { addChild() {} }
export class Image {}
export class Spacer {}
export class Text {}
`);

registerHooks({
	resolve(specifier, context, nextResolve) {
		if (specifier === "@earendil-works/pi-tui") {
			return { url: `data:text/javascript,${tuiModule}`, shortCircuit: true };
		}
		return nextResolve(specifier, context);
	},
});

const {
	default: laneMonitor,
	orchestratorInvocation,
	workflowIdFromToolResult,
	workflowToolIntent,
} = await import("../extensions/lane-monitor.ts");

async function waitFor(predicate: () => boolean): Promise<void> {
	for (let attempt = 0; attempt < 100; attempt += 1) {
		if (predicate()) return;
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	throw new Error("monitor poll did not finish");
}

test("orchestrator monitor invocation follows the launcher's Python control plane", () => {
	const previous = process.env.PI_LANE_CONTROL_PYTHON;
	try {
		process.env.PI_LANE_CONTROL_PYTHON = "/opt/pi-lane/bin/python";
		assert.deepEqual(orchestratorInvocation(["status", "--json"]), {
			command: "/opt/pi-lane/bin/python",
			args: ["-m", "pi_lane.orchestrator_cli", "status", "--json"],
		});
		process.env.PI_LANE_CONTROL_PYTHON = "relative/python";
		assert.throws(
			() => orchestratorInvocation(["status"]),
			/absolute executable path/,
		);
	} finally {
		if (previous === undefined) delete process.env.PI_LANE_CONTROL_PYTHON;
		else process.env.PI_LANE_CONTROL_PYTHON = previous;
	}
});

test("workflow tool intents claim only explicit current-protocol operations", () => {
	assert.deepEqual(
		workflowToolIntent("bash", { command: "pi-lanectl start --spec /tmp/design.json --json" }),
		{ action: "start" },
	);
	assert.deepEqual(
		workflowToolIntent("exec_command", { cmd: "./bin/pi-lanectl status --run-id workflow-7 --json" }),
		{ action: "status", runId: "workflow-7" },
	);
	assert.equal(workflowToolIntent("bash", { command: "pi-lanectl status --json" }), undefined);
	assert.equal(workflowToolIntent("bash", { command: "rtl-closure start --spec /tmp/design.json" }), undefined);
	assert.equal(workflowToolIntent("read", { command: "pi-lanectl start --spec /tmp/design.json" }), undefined);
	assert.equal(workflowIdFromToolResult({ workflow_id: "workflow-direct" }), "workflow-direct");
	assert.equal(
		workflowIdFromToolResult({
			content: [{ type: "text", text: '{"workflow_id":"workflow-streamed","status":"queued"}' }],
		}),
		"workflow-streamed",
	);
	assert.equal(workflowIdFromToolResult({ content: [{ type: "text", text: "workflow started" }] }), undefined);
});

test("a successful workflow start claims its returned ID despite concurrent active workflows", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const entries: Array<{ customType: string; data: any }> = [];
	const statuses: Array<string | undefined> = [];
	const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const snapshot = (workflowId: string) => ({
		workflow_id: workflowId,
		design: "fifo",
		pdk: "sky130A",
		status: "running",
		phase: "rtl_closure_running",
		revision: 1,
		completed_step_count: 0,
		elapsed_seconds: 1,
	});
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-${kind}`,
		data,
	});

	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry(customType: string, data: unknown) { entries.push({ customType, data }); },
		async exec(command: string, args: string[]) {
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl" && args[0] === "list") {
				return {
					stdout: envelope("run.list", [snapshot("workflow-old"), snapshot("workflow-new")]),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				const runId = args[args.indexOf("--run-id") + 1];
				return {
					stdout: envelope("run.get", snapshot(runId)),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				const runId = args[args.indexOf("--run-id") + 1];
				const after = Number(args[args.indexOf("--after-sequence") + 1]);
				return {
					stdout: envelope("run.events", {
						workflow_id: runId,
						after_sequence: after,
						next_after_sequence: after,
						has_more: false,
						events: [],
						cursor_sha256: emptyHash,
						acknowledged_sequence: after,
						snapshot: snapshot(runId),
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify() {},
			setStatus(_key: string, value: string | undefined) { statuses.push(value); },
			setWidget() {},
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-claim" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => calls.some((call) => call.command === "pi-lanectl"
		&& call.args[0] === "status" && call.args.includes("workflow-old")));
	const claimCallIndex = calls.length;
	await handlers.get("tool_execution_start")?.({
		type: "tool_execution_start",
		toolCallId: "tool-start",
		toolName: "bash",
		args: { command: "pi-lanectl start --spec /tmp/design.json --json" },
	}, context);
	await handlers.get("tool_execution_end")?.({
		type: "tool_execution_end",
		toolCallId: "tool-start",
		isError: false,
		result: { content: [{ type: "text", text: envelope("run.start", { workflow_id: "workflow-new" }) }] },
	}, context);
	await waitFor(() => calls.slice(claimCallIndex).some((call) => call.command === "pi-lanectl"
		&& call.args[0] === "status" && call.args.includes("workflow-new")));
	await commands.get("lane-monitor")?.handler("status", context);
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	const postClaimStatus = calls.slice(claimCallIndex).filter((call) => call.command === "pi-lanectl"
		&& call.args[0] === "status");
	assert.ok(postClaimStatus.length > 0);
	assert.ok(postClaimStatus.every((call) => call.args.includes("workflow-new")));
	assert.ok(statuses.some((status) => status?.includes("running")));
	const checkpoint = entries.findLast((entry) => entry.customType === "pi-lane-monitor-checkpoint");
	assert.equal(checkpoint?.data.workflowClaim, "workflow-new");
});

test("workflow monitoring acknowledges durable events and pins declared child runs", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const entries: Array<{ customType: string; data: any }> = [];
	const notices: string[] = [];
	let workflowStatusCalls = 0;
	let workflowEventCalls = 0;
	const firstHash = "a".repeat(64);
	const secondHash = "b".repeat(64);
	const workflowSnapshot = (revision: number) => ({
		workflow_id: "workflow-1",
		design: "fifo",
		pdk: "sky130A",
		status: "running",
		phase: revision === 1 ? "lane_running" : "formal_running",
		revision,
		rtl_closure_run_id: "rtl-1",
		lane_run_id: "lane-1",
		completed_step_count: revision,
		elapsed_seconds: revision * 10,
	});
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-${kind}`,
		data,
	});
	const event = (
		sequence: number,
		type: string,
		previousSha256: string | null,
		sha256: string,
	) => ({
		schema_version: 1,
		event_id: `workflow-1:${sequence}`,
		sequence,
		timestamp: `2026-07-28T12:00:0${sequence}+08:00`,
		source: "pi-lane-orchestrator",
		attempt_id: null,
		type,
		severity: "info",
		state_revision: sequence,
		phase: sequence === 1 ? "created" : "formal_running",
		payload: { status: "running", revision: sequence },
		failure: null,
		previous_sha256: previousSha256,
		sha256,
	});

	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry(customType: string, data: unknown) { entries.push({ customType, data }); },
		async exec(command: string, args: string[]) {
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl" && args[0] === "list") {
				return {
					stdout: envelope("run.list", [workflowSnapshot(1)]),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				workflowStatusCalls += 1;
				return {
					stdout: envelope("run.get", workflowSnapshot(workflowStatusCalls === 1 ? 1 : 2)),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				workflowEventCalls += 1;
				const first = workflowEventCalls === 1;
				const after = first ? 0 : 1;
				const row = first
					? event(1, "workflow.created", null, firstHash)
					: event(2, "workflow.progress", firstHash, secondHash);
				return {
					stdout: envelope("run.events", {
						workflow_id: "workflow-1",
						after_sequence: after,
						next_after_sequence: after + 1,
						has_more: false,
						events: [row],
						cursor_sha256: first ? firstHash : secondHash,
						acknowledged_sequence: after + 1,
						snapshot: workflowSnapshot(first ? 1 : 2),
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "rtl-closure") {
				return {
					stdout: JSON.stringify({
						run_id: "rtl-1",
						status: "running",
						phase: "lane_running",
						lane_run_id: "lane-1",
						events: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (args[0] === "events") {
				return {
					stdout: JSON.stringify({
						run_id: "lane-1",
						events: [],
						after_sequence: 0,
						next_after_sequence: 0,
						has_more: false,
						invalid_line_count: 0,
						generation: null,
						cursor_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
						snapshot: { status: "running", coarse_phase: "implementation" },
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (args[0] === "status") {
				return {
					stdout: JSON.stringify({
						run_id: "lane-1",
						status: "running",
						coarse_phase: "implementation",
						completed_step_count: 0,
						elapsed_seconds: 10,
						errors: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			return {
				stdout: "",
				stderr: `lane-agent: error: no managed ${args[0] === "pdk-status" ? "PDK" : "image"} installation exists`,
				code: 1,
				killed: false,
			};
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify(message: string) { notices.push(message); },
			setStatus() {},
			setWidget() {},
		},
		sessionManager: {
			getBranch: () => [],
			getSessionId: () => "session-workflow",
		},
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => entries.some((entry) => entry.customType === "pi-lane-monitor-checkpoint")
		&& calls.some((call) => call.command === "lane-agent" && call.args[0] === "status"));
	assert.equal(notices.some((notice) => notice.includes("workflow event #1")), false);
	await commands.get("lane-monitor")?.handler("follow workflow workflow-1", context);
	await waitFor(() => notices.some((notice) => notice.includes("workflow event #2")));
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.ok(calls.some((call) => call.command === "pi-lanectl"
		&& call.args.join(" ") === "list --active --limit 100 --json"));
	assert.ok(calls.some((call) => call.command === "pi-lanectl"
		&& call.args.join(" ") === "status --run-id workflow-1 --compact --json"));
	const eventCall = calls.find((call) => call.command === "pi-lanectl" && call.args[0] === "events");
	assert.ok(eventCall?.args.includes("--client-id"));
	assert.ok(eventCall?.args.includes("pi-lane-monitor:session-workflow"));
	assert.ok(eventCall?.args.includes("--acknowledge"));
	assert.ok(calls.some((call) => call.command === "rtl-closure"
		&& call.args.join(" ") === "report --run-id rtl-1 --compact --json"));
	assert.equal(calls.some((call) => call.command === "rtl-closure"
		&& call.args.join(" ") === "report --json"), false);
	assert.ok(notices.some((notice) => notice.includes("now follows workflow:workflow-1")));
	assert.ok(notices.some((notice) => notice.includes("workflow event #2")));
	const checkpoint = entries.findLast((entry) => entry.customType === "pi-lane-monitor-checkpoint");
	assert.equal(checkpoint?.data.schemaVersion, 6);
	assert.ok(Object.hasOwn(checkpoint?.data ?? {}, "rtlClosureEventCursors"));
	assert.equal(checkpoint?.data.bindings.workflow, "workflow-1");
	assert.equal(checkpoint?.data.workflowClaim, "workflow-1");
	assert.deepEqual(checkpoint?.data.pinnedSources, ["workflow"]);
	assert.equal(checkpoint?.data.workflowEventCursors["workflow-1"].afterSequence, 2);
});

test("a transient workflow status failure retains workflow ownership over a healthy child", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const statuses: Array<string | undefined> = [];
	const widgets: unknown[] = [];
	let workflowStatusCalls = 0;
	let laneStatusCalls = 0;
	const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const workflowSnapshot = {
		workflow_id: "workflow-transient",
		design: "fifo",
		pdk: "sky130A",
		status: "running",
		phase: "lane_running",
		revision: 1,
		lane_run_id: "lane-transient",
		completed_step_count: 1,
		elapsed_seconds: 10,
	};
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-${kind}`,
		data,
	});
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry() {},
		async exec(command: string, args: string[]) {
			if (command === "pi-lanectl" && args[0] === "list") {
				return { stdout: envelope("run.list", [workflowSnapshot]), stderr: "", code: 0, killed: false };
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				workflowStatusCalls += 1;
				if (workflowStatusCalls > 1) {
					return {
						stdout: JSON.stringify({
							schema_version: 1,
							protocol_version: 3,
							ok: false,
							kind: "error",
							request_id: "request-transient-error",
							error: {
								code: "ORCHESTRATOR_UNAVAILABLE",
								category: "infrastructure",
								message: "temporary control-plane failure",
								retryable: true,
							},
						}),
						stderr: "",
						code: 1,
						killed: false,
					};
				}
				return { stdout: envelope("run.get", workflowSnapshot), stderr: "", code: 0, killed: false };
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				const after = Number(args[args.indexOf("--after-sequence") + 1]);
				return {
					stdout: envelope("run.events", {
						workflow_id: "workflow-transient",
						after_sequence: after,
						next_after_sequence: after,
						has_more: false,
						events: [],
						cursor_sha256: emptyHash,
						acknowledged_sequence: after,
						snapshot: workflowSnapshot,
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "lane-agent" && args[0] === "events") {
				return {
					stdout: JSON.stringify({
						schema_version: 2,
						run_id: "lane-transient",
						events: [],
						after_sequence: 0,
						next_after_sequence: 0,
						has_more: false,
						invalid_line_count: 0,
						generation: null,
						cursor_sha256: emptyHash,
						snapshot: { status: "running", coarse_phase: "implementation" },
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "lane-agent" && args[0] === "status") {
				laneStatusCalls += 1;
				return {
					stdout: JSON.stringify({
						run_id: "lane-transient",
						status: "running",
						coarse_phase: "implementation",
						active_step: "OpenROAD.Place",
						completed_step_count: 1,
						elapsed_seconds: 10,
						errors: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify() {},
			setStatus(_key: string, value: string | undefined) { statuses.push(value); },
			setWidget(_key: string, value: unknown) { widgets.push(value); },
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-transient" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => workflowStatusCalls === 1 && laneStatusCalls === 1);
	await commands.get("lane-monitor")?.handler("status", context);
	const statusBeforeShutdown = statuses.at(-1);
	const visibleWidget = [...widgets].reverse().find((widget) => Array.isArray(widget)) as string[];
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.equal(workflowStatusCalls, 2);
	assert.equal(laneStatusCalls, 2, "the child may continue polling while its workflow owns display state");
	assert.ok(statusBeforeShutdown?.startsWith("Lane monitor: degraded | running"));
	assert.ok(visibleWidget.some((line) => line.includes("workflow-transient")));
	assert.equal(visibleWidget.some((line) => line.includes("run lane-transient")), false);
});

for (const actionableStatus of ["waiting_user", "blocked_infrastructure"] as const) {
test(`automatic workflow discovery preempts ${actionableStatus} but preserves explicit pins`, async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const statusRuns: string[] = [];
	let listCalls = 0;
	const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const snapshot = (workflowId: string, status: string, revision: number) => ({
		workflow_id: workflowId,
		design: workflowId === "workflow-new" ? "new_design" : "old_design",
		pdk: "sky130A",
		status,
		phase: status === "running" ? "lane_OpenROAD.CTS" : "failed",
		revision,
		completed_step_count: status === "running" ? 40 : 20,
		elapsed_seconds: status === "running" ? 30 : 300,
	});
	const oldWorkflow = snapshot("workflow-old", actionableStatus, 5);
	const newWorkflow = snapshot("workflow-new", "running", 2);
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-${kind}-${listCalls}`,
		data,
	});

	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry() {},
		async exec(command: string, args: string[]) {
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl" && args[0] === "list") {
				listCalls += 1;
				return {
					stdout: envelope("run.list", listCalls === 1
						? [oldWorkflow]
						: [oldWorkflow, newWorkflow]),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				const runId = args[2];
				statusRuns.push(runId);
				return {
					stdout: envelope("run.get", runId === "workflow-new" ? newWorkflow : oldWorkflow),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				const runId = args[2];
				const current = runId === "workflow-new" ? newWorkflow : oldWorkflow;
				return {
					stdout: envelope("run.events", {
						workflow_id: runId,
						after_sequence: 0,
						next_after_sequence: 0,
						has_more: false,
						events: [],
						cursor_sha256: emptyHash,
						acknowledged_sequence: 0,
						snapshot: current,
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "rtl-closure" || args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			return {
				stdout: "",
				stderr: `lane-agent: error: no managed ${args[0] === "pdk-status" ? "PDK" : "image"} installation exists`,
				code: 1,
				killed: false,
			};
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: { notify() {}, setStatus() {}, setWidget() {} },
		sessionManager: { getBranch: () => [], getSessionId: () => "session-preemption" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => statusRuns.includes("workflow-old"));
	await commands.get("lane-monitor")?.handler("status", context);
	await waitFor(() => statusRuns.includes("workflow-new"));
	assert.deepEqual(statusRuns.slice(0, 2), ["workflow-old", "workflow-new"]);
	assert.ok(calls.some((call) => call.command === "pi-lanectl"
		&& call.args.join(" ") === "list --active --limit 100 --json"));

	const listCallsBeforePin = listCalls;
	await commands.get("lane-monitor")?.handler("follow workflow workflow-old", context);
	await waitFor(() => statusRuns.filter((runId) => runId === "workflow-old").length >= 2);
	assert.equal(listCalls, listCallsBeforePin);
	assert.equal(statusRuns.at(-1), "workflow-old");
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);
});
}

test("automatic workflow monitoring retains completion and reports later integrity degradation", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const notices: string[] = [];
	let listCalls = 0;
	let statusCalls = 0;
	let eventCalls = 0;
	const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const eventHash = "c".repeat(64);
	const snapshot = (status: "running" | "completed" | "failed", revision: number) => ({
		workflow_id: "workflow-integrity",
		design: "integrity_case",
		pdk: "sky130A",
		status,
		phase: status === "failed" ? "terminal_integrity_audit_failed" : status,
		revision,
		failure: status === "failed" ? {
			code: "RELEASE_INTEGRITY/PUBLICATION_INVALID",
			domain: "orchestration",
			message: "published evidence changed",
			retryable: false,
			repairable_by_rtl: false,
			user_action_required: true,
		} : undefined,
	});
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-integrity-${kind}`,
		data,
	});
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry() {},
		async exec(command: string, args: string[]) {
			if (command === "pi-lanectl" && args[0] === "list") {
				listCalls += 1;
				return {
					stdout: envelope("run.list", listCalls === 1 ? [snapshot("running", 1)] : []),
					stderr: "", code: 0, killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				statusCalls += 1;
				const value = statusCalls === 1
					? snapshot("running", 1)
					: statusCalls === 2
						? snapshot("completed", 2)
						: snapshot("failed", 3);
				return { stdout: envelope("run.get", value), stderr: "", code: 0, killed: false };
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				eventCalls += 1;
				const degraded = eventCalls >= 3;
				return {
					stdout: envelope("run.events", {
						workflow_id: "workflow-integrity",
						after_sequence: 0,
						next_after_sequence: degraded ? 1 : 0,
						has_more: false,
						events: degraded ? [{
							schema_version: 1,
							event_id: "workflow-integrity:1",
							sequence: 1,
							timestamp: "2026-07-30T16:00:00+08:00",
							source: "pi-lane-orchestrator",
							attempt_id: null,
							type: "workflow.integrity_degraded",
							severity: "error",
							state_revision: 3,
							phase: "terminal_integrity_audit_failed",
							payload: { status: "failed", revision: 3 },
							failure: snapshot("failed", 3).failure,
							previous_sha256: null,
							sha256: eventHash,
						}] : [],
						cursor_sha256: degraded ? eventHash : emptyHash,
						acknowledged_sequence: degraded ? 1 : 0,
						snapshot: degraded ? snapshot("failed", 3) : (
							eventCalls === 1 ? snapshot("running", 1) : snapshot("completed", 2)
						),
					}),
					stderr: "", code: 0, killed: false,
				};
			}
			if (command === "rtl-closure" || args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			return {
				stdout: "",
				stderr: `lane-agent: error: no managed ${args[0] === "pdk-status" ? "PDK" : "image"} installation exists`,
				code: 1,
				killed: false,
			};
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: { notify(message: string) { notices.push(message); }, setStatus() {}, setWidget() {} },
		sessionManager: { getBranch: () => [], getSessionId: () => "session-integrity" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => statusCalls >= 1);
	await commands.get("lane-monitor")?.handler("status", context);
	await waitFor(() => statusCalls >= 2);
	await commands.get("lane-monitor")?.handler("status", context);
	await waitFor(() => statusCalls >= 3);
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.equal(statusCalls, 3);
	assert.ok(listCalls >= 2);
	assert.ok(notices.some((notice) => notice.includes("workflow.integrity_degraded")));
});

test("automatic terminal workflow watch is restored from a session checkpoint", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const entries: Array<{ customType: string; data: any }> = [];
	const workflow = {
		workflow_id: "workflow-restored-terminal",
		design: "restored_terminal",
		pdk: "sky130A",
		status: "completed",
		phase: "completed",
		revision: 9,
	};
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-restored-${kind}`,
		data,
	});
	const branch = [{
		type: "custom",
		customType: "pi-lane-monitor-checkpoint",
		data: {
			schemaVersion: 6,
			workflowAutoDiscovery: true,
			workflowClaim: "workflow-restored-terminal",
			recentTerminalWorkflow: "workflow-restored-terminal",
			bindings: {},
			pinnedSources: [],
			workflowEventCursors: {
				"workflow-restored-terminal": {
					afterSequence: 0,
					suppressHistory: false,
				},
			},
			rtlClosureEventCursors: {},
			laneEventCursors: {},
		},
	}];
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand() {},
		appendEntry(customType: string, data: any) { entries.push({ customType, data }); },
		async exec(command: string, args: string[]) {
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl" && args[0] === "list") {
				return { stdout: envelope("run.list", []), stderr: "", code: 0, killed: false };
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				return { stdout: envelope("run.get", workflow), stderr: "", code: 0, killed: false };
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				return {
					stdout: envelope("run.events", {
						workflow_id: workflow.workflow_id,
						after_sequence: 0,
						next_after_sequence: 0,
						has_more: false,
						events: [],
						cursor_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
						acknowledged_sequence: 0,
						snapshot: workflow,
					}),
					stderr: "", code: 0, killed: false,
				};
			}
			if (command === "rtl-closure" || args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			return {
				stdout: "",
				stderr: "lane-agent: error: no managed installation exists",
				code: 1,
				killed: false,
			};
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: { notify() {}, setStatus() {}, setWidget() {} },
		sessionManager: {
			getBranch: () => branch,
			getSessionId: () => "session-restored-terminal",
		},
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "reload" }, context);
	await waitFor(() => calls.some((call) => call.command === "pi-lanectl"
		&& call.args[0] === "status"
		&& call.args.includes("workflow-restored-terminal"))
		&& calls.some((call) => call.command === "pi-lanectl"
			&& call.args[0] === "events"
			&& call.args.includes("workflow-restored-terminal")));
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.equal(branch[0].data.recentTerminalWorkflow, "workflow-restored-terminal");
	assert.deepEqual(branch[0].data.pinnedSources, []);
});

test("monitor binds an RTL-Closure child by explicit run ID and checkpoints Lane events", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const entries: Array<{ customType: string; data: unknown }> = [];
	const notices: string[] = [];

	const pi = {
		on(event: string, handler: (...args: any[]) => any) {
			handlers.set(event, handler);
		},
		registerEntryRenderer() {},
		registerCommand() {},
		appendEntry(customType: string, data: unknown) {
			entries.push({ customType, data });
		},
		async exec(command: string, args: string[], options: { signal?: AbortSignal }) {
			if (options.signal?.aborted) throw new Error("aborted");
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl" && args[0] === "list") {
				return {
					stdout: JSON.stringify({
						schema_version: 1,
						protocol_version: 3,
						ok: true,
						kind: "run.list",
						request_id: "request-1",
						data: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "rtl-closure" && args[0] === "list") {
				return {
					stdout: JSON.stringify([{
						run_id: "rtl-1",
						status: "running",
						phase: "lane_running",
					}]),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "rtl-closure") {
				return {
					stdout: JSON.stringify({
						run_id: "rtl-1",
						status: "running",
						phase: "lane_running",
						lane_run_id: "lane-1",
						events: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (args[0] === "events") {
				return {
					stdout: JSON.stringify({
						schema_version: 2,
						run_id: "lane-1",
						events: [
							{ sequence: 1, event: "step_started", ordinal: 1, step: "Verilator.Lint" },
							{ sequence: 2, event: "step_completed", ordinal: 1, step: "Verilator.Lint" },
						],
						after_sequence: 0,
						next_after_sequence: 2,
						has_more: false,
						invalid_line_count: 0,
						generation: "1:2",
						cursor_sha256: "a".repeat(64),
						snapshot: {
							status: "running",
							coarse_phase: "implementation",
							active_step: "Verilator.Lint",
							completed_step_count: 1,
							elapsed_seconds: 3,
							errors: [],
						},
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (args[0] === "status") {
				return {
					stdout: JSON.stringify({
						run_id: "lane-1",
						status: "running",
						coarse_phase: "implementation",
						active_step: "Verilator.Lint",
						completed_step_count: 1,
						elapsed_seconds: 3,
						errors: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			return {
				stdout: "",
				stderr: `lane-agent: error: no managed ${args[0] === "pdk-status" ? "PDK" : "image"} installation exists`,
				code: 1,
				killed: false,
			};
		},
	};

	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify(message: string) { notices.push(message); },
			setStatus() {},
			setWidget() {},
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-child" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => entries.some((entry) => entry.customType === "pi-lane-monitor-checkpoint"));
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	const laneCalls = calls.filter((call) => call.command === "lane-agent");
	assert.ok(laneCalls.some((call) => call.args.join(" ").startsWith("events --run-id lane-1 ")));
	assert.ok(laneCalls.some((call) => call.args.join(" ") === "status --run-id lane-1 --json"));
	assert.equal(
		laneCalls.some((call) => call.args.join(" ") === "status --json"),
		false,
		"an RTL-Closure child must never be replaced by global-current Lane state",
	);
	assert.equal(entries.some((entry) => entry.customType === "pi-lane-step"), false);
	const checkpoint = entries.findLast((entry) => entry.customType === "pi-lane-monitor-checkpoint");
	assert.deepEqual((checkpoint?.data as any).bindings, {});
	assert.deepEqual((checkpoint?.data as any).pinnedSources, []);
	assert.equal((checkpoint?.data as any).laneEventCursors["lane-1"].afterSequence, 2);
	assert.equal((checkpoint?.data as any).laneEventCursors["lane-1"].suppressHistory, false);
	assert.equal(notices.some((notice) => notice.includes("Step started")), false);
});

test("a Lane child that appears after live workflow attachment reports its existing events", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const entries: Array<{ customType: string; data: any }> = [];
	const notices: string[] = [];
	let workflowStatusCalls = 0;
	const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const workflowSnapshot = () => ({
		workflow_id: "workflow-live-child",
		design: "fifo",
		pdk: "sky130A",
		status: "running",
		phase: workflowStatusCalls >= 2 ? "lane_running" : "rtl_closure_running",
		revision: workflowStatusCalls,
		lane_run_id: workflowStatusCalls >= 2 ? "lane-live-child" : null,
		completed_step_count: 0,
		elapsed_seconds: workflowStatusCalls,
	});
	const envelope = (kind: string, data: unknown) => JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind,
		request_id: `request-${kind}`,
		data,
	});
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry(customType: string, data: unknown) { entries.push({ customType, data }); },
		async exec(command: string, args: string[]) {
			if (command === "pi-lanectl" && args[0] === "list") {
				return {
					stdout: envelope("run.list", [workflowSnapshot()]),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "status") {
				workflowStatusCalls += 1;
				return {
					stdout: envelope("run.get", workflowSnapshot()),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "pi-lanectl" && args[0] === "events") {
				const after = Number(args[args.indexOf("--after-sequence") + 1]);
				return {
					stdout: envelope("run.events", {
						workflow_id: "workflow-live-child",
						after_sequence: after,
						next_after_sequence: after,
						has_more: false,
						events: [],
						cursor_sha256: emptyHash,
						acknowledged_sequence: after,
						snapshot: workflowSnapshot(),
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "lane-agent" && args[0] === "events") {
				return {
					stdout: JSON.stringify({
						schema_version: 2,
						run_id: "lane-live-child",
						events: [
							{ sequence: 1, event: "step_started", ordinal: 1, step: "Verilator.Lint" },
							{ sequence: 2, event: "step_completed", ordinal: 1, step: "Verilator.Lint" },
						],
						after_sequence: 0,
						next_after_sequence: 2,
						has_more: false,
						invalid_line_count: 0,
						generation: "1:2",
						cursor_sha256: "a".repeat(64),
						snapshot: {
							status: "running",
							coarse_phase: "implementation",
							active_step: "Verilator.Lint",
							completed_step_count: 1,
							elapsed_seconds: 3,
							errors: [],
						},
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "lane-agent" && args[0] === "status") {
				return {
					stdout: JSON.stringify({
						run_id: "lane-live-child",
						status: "running",
						coarse_phase: "implementation",
						active_step: "Verilator.Lint",
						completed_step_count: 1,
						elapsed_seconds: 3,
						errors: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify(message: string) { notices.push(message); },
			setStatus() {},
			setWidget() {},
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-live-child" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => workflowStatusCalls === 1);
	await commands.get("lane-monitor")?.handler("status", context);
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.ok(entries.some((entry) => entry.customType === "pi-lane-step"));
	assert.ok(notices.some((notice) => notice.includes("Step started") && notice.includes("Verilator.Lint")));
	const checkpoint = entries.findLast((entry) => entry.customType === "pi-lane-monitor-checkpoint");
	assert.equal(checkpoint?.data.laneEventCursors["lane-live-child"].afterSequence, 2);
	assert.equal(checkpoint?.data.laneEventCursors["lane-live-child"].suppressHistory, false);
});

test("session replacement aborts an old poll and blocks stale notifications", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	let firstSignal: AbortSignal | undefined;
	let calls = 0;
	const oldNotices: string[] = [];
	const newNotices: string[] = [];

	const pi = {
		on(event: string, handler: (...args: any[]) => any) { handlers.set(event, handler); },
		registerEntryRenderer() {},
		registerCommand() {},
		appendEntry() {},
		async exec(command: string, args: string[], options: { signal?: AbortSignal }) {
			calls += 1;
			if (calls === 1) {
				firstSignal = options.signal;
				return await new Promise((_resolve, reject) => {
					options.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
				});
			}
			if (command === "pi-lanectl" && args[0] === "list") {
				return {
					stdout: JSON.stringify({
						schema_version: 1,
						protocol_version: 3,
						ok: true,
						kind: "run.list",
						request_id: "request-2",
						data: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "rtl-closure" && args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			if (command === "lane-agent" && args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			const label = command === "rtl-closure"
				? "no RTL-Closure Lane run exists"
				: args[0] === "status"
					? "no managed LibreLane run exists"
					: `no managed ${args[0] === "pdk-status" ? "PDK" : "image"} installation exists`;
			return { stdout: "", stderr: label, code: 1, killed: false };
		},
	};
	const makeContext = (notices: string[]) => ({
		hasUI: true,
		mode: "tui",
		ui: {
			notify(message: string) { notices.push(message); },
			setStatus() {},
			setWidget() {},
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-replace" },
	});
	const oldContext = makeContext(oldNotices);
	const newContext = makeContext(newNotices);

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, oldContext);
	await waitFor(() => firstSignal !== undefined);
	await handlers.get("session_start")?.({ type: "session_start", reason: "reload" }, newContext);
	assert.equal(firstSignal?.aborted, true);
	await waitFor(() => calls >= 5);
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, newContext);
	assert.deepEqual(oldNotices, []);
});

test("automatic discovery attaches an active run and releases it after completion", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const statuses: Array<string | undefined> = [];
	const widgets: unknown[] = [];
	let reportCalls = 0;
	let chipListCalls = 0;
	const workflowList = JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind: "run.list",
		request_id: "request-auto",
		data: [],
	});
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry() {},
		async exec(command: string, args: string[]) {
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl") {
				return { stdout: workflowList, stderr: "", code: 0, killed: false };
			}
			if (command === "rtl-closure" && args[0] === "list") {
				chipListCalls += 1;
				return {
					stdout: JSON.stringify(chipListCalls === 1
						? [{ run_id: "rtl-auto", status: "running", phase: "generating" }]
						: []),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (command === "rtl-closure" && args[0] === "report") {
				reportCalls += 1;
				const completed = reportCalls >= 2;
				return {
					stdout: JSON.stringify({
						run_id: "rtl-auto",
						status: completed ? "completed" : "running",
						phase: completed ? "completed" : "generating",
						events: [],
					}),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			if (args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			return {
				stdout: "",
				stderr: `lane-agent: error: no managed ${args[0] === "pdk-status" ? "PDK" : "image"} installation exists`,
				code: 1,
				killed: false,
			};
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify() {},
			setStatus(_key: string, value: string | undefined) { statuses.push(value); },
			setWidget(_key: string, value: unknown) { widgets.push(value); },
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-auto" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => reportCalls === 1);
	await commands.get("lane-monitor")?.handler("status", context);
	assert.equal(reportCalls, 2);
	assert.ok(statuses.includes("Lane: completed | completed"));
	await handlers.get("agent_start")?.({ type: "agent_start" }, context);
	assert.equal(statuses.at(-1), "Lane: agent | processing request");
	await commands.get("lane-monitor")?.handler("status", context);
	assert.equal(
		statuses.at(-1),
		"Lane: agent | processing request",
		"polling while the agent is busy must not restore an old terminal run",
	);
	await handlers.get("agent_settled")?.({ type: "agent_settled" }, context);
	assert.equal(statuses.at(-1), "Lane monitor: idle");
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.equal(chipListCalls, 2);
	assert.ok(calls.every((call) => call.command !== "rtl-closure"
		|| call.args.join(" ") !== "report --json"));
	assert.ok(statuses.includes("Lane monitor: idle"));
	assert.equal(widgets.at(-1), undefined);
});

test("manual install follow uses the install status CLI contract", async () => {
	const handlers = new Map<string, (...args: any[]) => any>();
	const commands = new Map<string, { handler: (...args: any[]) => Promise<void> }>();
	const calls: Array<{ command: string; args: string[] }> = [];
	const notices: string[] = [];
	const workflowList = JSON.stringify({
		schema_version: 1,
		protocol_version: 3,
		ok: true,
		kind: "run.list",
		request_id: "request-install-follow",
		data: [],
	});
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerEntryRenderer() {},
		registerCommand(name: string, command: { handler: (...args: any[]) => Promise<void> }) {
			commands.set(name, command);
		},
		appendEntry() {},
		async exec(command: string, args: string[]) {
			calls.push({ command, args: [...args] });
			if (command === "pi-lanectl") {
				return { stdout: workflowList, stderr: "", code: 0, killed: false };
			}
			if (command === "rtl-closure" || args[0] === "list") {
				return { stdout: "[]", stderr: "", code: 0, killed: false };
			}
			if (args[0] === "pdk-status") {
				return {
					stdout: JSON.stringify({ run_id: "pdk-run", status: "running" }),
					stderr: "",
					code: 0,
					killed: false,
				};
			}
			return {
				stdout: "",
				stderr: "lane-agent: error: no managed image installation exists",
				code: 1,
				killed: false,
			};
		},
	};
	const context = {
		hasUI: true,
		mode: "tui",
		ui: {
			notify(message: string) { notices.push(message); },
			setStatus() {},
			setWidget() {},
		},
		sessionManager: { getBranch: () => [], getSessionId: () => "session-install-follow" },
	};

	laneMonitor(pi as any);
	await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, context);
	await waitFor(() => calls.some((call) => call.command === "lane-agent"
		&& call.args.join(" ") === "pdk-status"));
	await commands.get("lane-monitor")?.handler("follow pdk pdk-run", context);
	await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "quit" }, context);

	assert.ok(calls.some((call) => call.command === "lane-agent"
		&& call.args.join(" ") === "pdk-status --run-id pdk-run"));
	assert.equal(calls.some((call) => call.command === "lane-agent"
		&& call.args[0] === "pdk-status" && call.args.includes("--json")), false);
	assert.ok(notices.some((notice) => notice.includes("now follows pdk:pdk-run")));
});

function argsIsStatus(args: string[]): boolean {
	return args[0] === "status" || args[0] === "events";
}
