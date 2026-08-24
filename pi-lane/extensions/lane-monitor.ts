import type {
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Box, Image, Spacer, Text } from "@earendil-works/pi-tui";
import { closeSync, fstatSync, openSync, readSync } from "node:fs";
import { basename, isAbsolute } from "node:path";
import {
	rtlClosureSnapshot,
	compactNotice,
	drainRtlClosureEvents,
	eventNoticeType,
	eventSummary,
	fingerprint,
	heartbeatDue,
	heartbeatSummary,
	initialLaneEventCursor,
	initialWorkflowEventCursor,
	installSnapshot,
	isManagedStateAbsent,
	jsonObjectPayload,
	laneEventReport,
	laneSnapshot,
	monitorCheckpoint,
	monitorRunId,
	nextMonitorHealth,
	orchestratorEnvelope,
	orchestratorListEnvelope,
	sourceRunKey,
	statusType,
	summary,
	widgetLines,
	workflowEventNoticeType,
	workflowEventReport,
	workflowEventSummary,
	workflowSnapshot,
} from "./lane-monitor-core.ts";
import type {
	LaneEventCursor,
	LaneTimelineEvent,
	MonitorCheckpoint,
	MonitorHealth,
	MonitorSource,
	Snapshot,
	StepRecord,
	WorkflowEvent,
	WorkflowEventCursor,
} from "./lane-monitor-core.ts";

interface StepEntryData {
	runId: string;
	ordinal: number;
	step: string;
	phase: string;
	status: string;
	directory: string;
	commands?: string;
	log?: string;
	runtime?: string;
	image?: string;
	visualizationOnly: boolean;
	timestamp: number;
}

export function orchestratorInvocation(args: string[]): { command: string; args: string[] } {
	const python = process.env.PI_LANE_CONTROL_PYTHON;
	if (python === undefined) return { command: "pi-lanectl", args };
	if (!isAbsolute(python) || python.length > 4096 || python.includes("\0")) {
		throw new Error("PI_LANE_CONTROL_PYTHON must be an absolute executable path");
	}
	return {
		command: python,
		args: ["-m", "pi_lane.orchestrator_cli", ...args],
	};
}

const TERMINAL = new Set(["completed", "failed", "incomplete", "stopped", "cancelled"]);
const DISCOVERABLE = new Set([
	"created",
	"queued",
	"running",
	"waiting_user",
	"blocked_infrastructure",
]);
const ACTIONABLE = new Set(["waiting_user", "blocked_infrastructure"]);
const WORKFLOW_STATUS_PRIORITY: Record<string, number> = {
	running: 3,
	queued: 2,
	created: 1,
	waiting_user: 0,
	blocked_infrastructure: 0,
};
const DEFAULT_INTERVAL_MS = 10_000;
const DEFAULT_HEARTBEAT_INTERVAL_MS = 60_000;

interface JsonCommandOutcome {
	value?: Record<string, unknown>;
	error?: string;
	absent?: boolean;
}

interface JsonArrayCommandOutcome {
	values?: Record<string, unknown>[];
	error?: string;
}

interface CheckpointEntryData extends MonitorCheckpoint {
	savedAt: number;
}

type AttachmentMode = "history" | "live";

interface WorkflowToolIntent {
	action: "start" | "status";
	runId?: string;
}

const CHECKPOINT_ENTRY = "pi-lane-monitor-checkpoint";
const SOURCES: MonitorSource[] = ["workflow", "rtl_closure", "lane", "pdk", "image"];
const MAX_EVENT_PAGES_PER_POLL = 4;
const EVENT_PAGE_SIZE = 1_000;
const SOURCE_PRIORITY: Record<Snapshot["kind"], number> = {
	workflow: 5,
	rtl_closure: 4,
	lane: 3,
	pdk: 2,
	image: 1,
};
const FILE_PREVIEW_CACHE = new Map<string, Buffer>();

export function workflowToolIntent(toolName: string, args: unknown): WorkflowToolIntent | undefined {
	if (!/^(?:bash|exec_command)$/i.test(toolName)) return undefined;
	if (!args || typeof args !== "object" || Array.isArray(args)) return undefined;
	const raw = args as Record<string, unknown>;
	const command = typeof raw.command === "string"
		? raw.command
		: typeof raw.cmd === "string"
			? raw.cmd
			: undefined;
	if (!command || !/(?:^|[\s;&|])(?:[^\s;&|]*\/)?pi-lanectl\s+/m.test(command)) {
		return undefined;
	}
	if (/(?:^|[\s;&|])(?:[^\s;&|]*\/)?pi-lanectl\s+start(?:\s|$)/m.test(command)) {
		return { action: "start" };
	}
	if (/(?:^|[\s;&|])(?:[^\s;&|]*\/)?pi-lanectl\s+status(?:\s|$)/m.test(command)) {
		const match = command.match(/--run-id(?:=|\s+)["']?([A-Za-z0-9][A-Za-z0-9_.-]{0,199})/);
		return match ? { action: "status", runId: match[1] } : undefined;
	}
	return undefined;
}

function toolResultText(result: unknown): string {
	if (typeof result === "string") return result;
	if (!result || typeof result !== "object" || Array.isArray(result)) return "";
	const raw = result as Record<string, unknown>;
	if (!Array.isArray(raw.content)) return "";
	return raw.content.flatMap((item) => {
		if (!item || typeof item !== "object" || Array.isArray(item)) return [];
		const text = (item as Record<string, unknown>).text;
		return typeof text === "string" ? [text] : [];
	}).join("\n");
}

export function workflowIdFromToolResult(result: unknown): string | undefined {
	const direct = result && typeof result === "object" && !Array.isArray(result)
		? monitorRunId((result as Record<string, unknown>).workflow_id)
		: undefined;
	if (direct) return direct;
	const text = toolResultText(result);
	const match = text.match(/["']workflow_id["']\s*:\s*["']([A-Za-z0-9][A-Za-z0-9_.-]{0,199})["']/);
	return monitorRunId(match?.[1]);
}

function boundedFile(
	path: string,
	maximumBytes: number,
	mode: "head" | "tail" | "whole",
): Buffer {
	const descriptor = openSync(path, "r");
	try {
		const metadata = fstatSync(descriptor);
		if (!metadata.isFile()) throw new Error("preview path is not a regular file");
		if (mode === "whole" && metadata.size > maximumBytes) {
			throw new Error(`preview file exceeds ${maximumBytes} bytes`);
		}
		const length = Math.min(metadata.size, maximumBytes);
		const offset = mode === "tail" ? Math.max(0, metadata.size - length) : 0;
		const key = `${path}:${metadata.mtimeMs}:${metadata.size}:${mode}:${maximumBytes}`;
		const cached = FILE_PREVIEW_CACHE.get(key);
		if (cached) return cached;
		const content = Buffer.alloc(length);
		let consumed = 0;
		while (consumed < length) {
			const count = readSync(descriptor, content, consumed, length - consumed, offset + consumed);
			if (count === 0) break;
			consumed += count;
		}
		const result = consumed === length ? content : content.subarray(0, consumed);
		FILE_PREVIEW_CACHE.set(key, result);
		while (FILE_PREVIEW_CACHE.size > 32) {
			const oldest = FILE_PREVIEW_CACHE.keys().next().value;
			if (oldest === undefined) break;
			FILE_PREVIEW_CACHE.delete(oldest);
		}
		return result;
	} finally {
		closeSync(descriptor);
	}
}

function pngPreview(path: string): Buffer {
	const content = boundedFile(path, 8 * 1024 * 1024, "whole");
	const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
	if (content.length < signature.length || !content.subarray(0, 8).equals(signature)) {
		throw new Error("preview image is not a PNG file");
	}
	return content;
}

function intervalMs(): number {
	const parsed = Number.parseInt(process.env.PI_LANE_MONITOR_INTERVAL_MS ?? "", 10);
	if (!Number.isFinite(parsed)) return DEFAULT_INTERVAL_MS;
	return Math.max(3_000, Math.min(60_000, parsed));
}

function heartbeatIntervalMs(): number {
	const parsed = Number.parseInt(
		process.env.PI_LANE_HEARTBEAT_INTERVAL_MS ?? "",
		10,
	);
	if (!Number.isFinite(parsed)) return DEFAULT_HEARTBEAT_INTERVAL_MS;
	return Math.max(10_000, Math.min(15 * 60_000, parsed));
}

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

async function jsonCommand(
	pi: ExtensionAPI,
	command: string,
	args: string[],
	signal?: AbortSignal,
): Promise<JsonCommandOutcome> {
	const label = `${command} ${args[0] ?? ""}`.trim();
	try {
		const result = await pi.exec(command, args, { timeout: 8_000, signal });
		const stdout = result.stdout.trim();
		let parseError: string | undefined;
		if (stdout) {
			try {
				const value = jsonObjectPayload(JSON.parse(stdout) as unknown);
				if (value) return { value };
				parseError = "JSON response is not a successful object envelope";
			} catch (error) {
				parseError = `invalid JSON: ${errorMessage(error)}`;
			}
		}
		const diagnostic = (result.stderr.trim() || stdout).slice(0, 800);
		if (isManagedStateAbsent(diagnostic)) return { absent: true };
		return {
			error: `${label}: ${parseError ?? (diagnostic || `exit code ${result.code}`)}`,
		};
	} catch (error) {
		return { error: `${label}: ${errorMessage(error).slice(0, 800)}` };
	}
}

async function orchestratorJsonCommand(
	pi: ExtensionAPI,
	args: string[],
	expectedKind: "run.get" | "run.events",
	signal?: AbortSignal,
): Promise<JsonCommandOutcome> {
	const label = `pi-lanectl ${args[0] ?? ""}`.trim();
	try {
		const invocation = orchestratorInvocation(args);
		const result = await pi.exec(invocation.command, invocation.args, { timeout: 12_000, signal });
		const stdout = result.stdout.trim();
		if (stdout) {
			try {
				const envelope = orchestratorEnvelope(JSON.parse(stdout) as unknown, expectedKind);
				if (!envelope) {
					return { error: `${label}: invalid orchestrator envelope` };
				}
				if (envelope.value) return { value: envelope.value };
				if (envelope.errorCode === "RUN_NOT_FOUND") return { absent: true };
				return {
					error: `${label}: ${envelope.errorCode ?? "UNKNOWN"}: `
						+ `${envelope.errorMessage ?? "orchestrator request failed"}`
						+ ` (${envelope.errorCategory ?? "orchestration"}, `
						+ `retryable=${envelope.retryable ?? false})`,
				};
			} catch (error) {
				return { error: `${label}: invalid JSON: ${errorMessage(error)}` };
			}
		}
		const diagnostic = result.stderr.trim().slice(0, 800);
		return { error: `${label}: ${diagnostic || `exit code ${result.code}`}` };
	} catch (error) {
		return { error: `${label}: ${errorMessage(error).slice(0, 800)}` };
	}
}

async function orchestratorListCommand(
	pi: ExtensionAPI,
	args: string[],
	signal?: AbortSignal,
): Promise<JsonArrayCommandOutcome> {
	const label = `pi-lanectl ${args[0] ?? ""}`.trim();
	try {
		const invocation = orchestratorInvocation(args);
		const result = await pi.exec(invocation.command, invocation.args, { timeout: 12_000, signal });
		const stdout = result.stdout.trim();
		if (stdout) {
			try {
				const envelope = orchestratorListEnvelope(JSON.parse(stdout) as unknown);
				if (!envelope) return { error: `${label}: invalid orchestrator run.list envelope` };
				if (envelope.values) return { values: envelope.values };
				return {
					error: `${label}: ${envelope.errorCode ?? "UNKNOWN"}: `
						+ `${envelope.errorMessage ?? "orchestrator request failed"}`
						+ ` (${envelope.errorCategory ?? "orchestration"}, `
						+ `retryable=${envelope.retryable ?? false})`,
				};
			} catch (error) {
				return { error: `${label}: invalid JSON: ${errorMessage(error)}` };
			}
		}
		const diagnostic = result.stderr.trim().slice(0, 800);
		return { error: `${label}: ${diagnostic || `exit code ${result.code}`}` };
	} catch (error) {
		return { error: `${label}: ${errorMessage(error).slice(0, 800)}` };
	}
}

function preferredWorkflowRunId(values: Record<string, unknown>[]): string | undefined {
	let selected: string | undefined;
	let selectedPriority = -1;
	for (const value of values) {
		const runId = monitorRunId(value.workflow_id);
		const status = typeof value.status === "string" ? value.status : undefined;
		if (!runId || !status || !DISCOVERABLE.has(status)) continue;
		const priority = WORKFLOW_STATUS_PRIORITY[status] ?? -1;
		if (priority > selectedPriority) {
			selected = runId;
			selectedPriority = priority;
		}
	}
	return selected;
}

function newlyStartedWorkflowRunId(
	values: Record<string, unknown>[],
	previous: string | undefined,
): string | undefined {
	for (const value of values) {
		const runId = monitorRunId(value.workflow_id);
		const status = typeof value.status === "string" ? value.status : undefined;
		if (runId && runId !== previous && status && DISCOVERABLE.has(status)) return runId;
	}
	return undefined;
}

export default function laneMonitor(pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | undefined;
	let context: ExtensionContext | undefined;
	let activePoll: { generation: number; promise: Promise<void> } | undefined;
	let sessionGeneration = 0;
	let sessionAbort: AbortController | undefined;
	let enabled = true;
	const fingerprints = new Map<string, string>();
	const announcedSteps = new Map<string, Set<number>>();
	const announcedVisualizations = new Map<string, Set<number>>();
	const workflowEventCursors = new Map<string, WorkflowEventCursor>();
	const rtlClosureEventCursor = new Map<string, number>();
	const laneEventCursors = new Map<string, LaneEventCursor>();
	const laneEventSteps = new Map<string, Map<number, StepRecord>>();
	const liveAttachments = new Set<string>();
	const bindings: Partial<Record<MonitorSource, string>> = {};
	const pinnedSources = new Set<MonitorSource>();
	const terminalBySource = new Map<MonitorSource, boolean>();
	const healthBySourceRun = new Map<string, MonitorHealth>();
	const lastHeartbeatAt = new Map<string, number>();
	let visible: Snapshot | undefined;
	let latestLane: Snapshot | undefined;
	let monitorClientId: string | undefined;
	let workflowAutoDiscovery = true;
	let workflowClaim: string | undefined;
	let workflowClaimPending = false;
	let pendingWorkflowClaimPrevious: string | undefined;
	let workflowBindingStatus: string | undefined;
	let recentTerminalWorkflow: string | undefined;
	let checkpointFingerprint = "";
	let agentBusy = false;
	let agentActivity = "processing request";
	let busyTerminalWorkflow: string | undefined;
	const activeAgentTools = new Map<string, string>();
	const workflowToolIntents = new Map<string, WorkflowToolIntent>();

	pi.registerEntryRenderer<CheckpointEntryData>(CHECKPOINT_ENTRY, () => undefined);

	function setRecentCursor<T>(map: Map<string, T>, runId: string, value: T) {
		map.delete(runId);
		map.set(runId, value);
		while (map.size > 256) {
			const protectedIds = new Set(Object.values(bindings));
			const removable = [...map.keys()].find((candidate) => !protectedIds.has(candidate));
			if (!removable) break;
			map.delete(removable);
		}
	}

	pi.registerEntryRenderer<StepEntryData>(
		"pi-lane-step",
		(entry, { expanded }, theme) => {
			const data = entry.data;
			if (!data) return undefined;
			const box = new Box(1, 1, (text) => theme.bg("customMessageBg", text));
			const label = data.visualizationOnly ? "LAYOUT PREVIEW" : "STEP COMPLETED";
			box.addChild(new Text(
				`${theme.fg("success", `[${label}]`)} ${String(data.ordinal).padStart(2, "0")} ${data.step}`,
				0,
				0,
			));
			box.addChild(new Text(
				theme.fg("dim", `run ${data.runId} | phase ${data.phase} | ${data.status}`),
				0,
				0,
			));
			if (data.commands) {
				box.addChild(new Text(`commands: ${data.commands}`, 0, 0));
			}
			if (data.log) box.addChild(new Text(`log: ${data.log}`, 0, 0));
			if (data.runtime) box.addChild(new Text(`runtime: ${data.runtime}`, 0, 0));

			if (expanded && data.commands) {
				try {
					const commandText = boundedFile(data.commands, 64 * 1024, "head")
						.toString("utf8")
						.split("\n")
						.slice(0, 40)
						.join("\n");
					if (commandText.trim()) {
						box.addChild(new Spacer(1));
						box.addChild(new Text(theme.fg("muted", commandText), 0, 0));
					}
				} catch {
					// The path remains visible even if a historical file was removed.
				}
			}
			if (expanded && data.log) {
				try {
					const logLines = boundedFile(data.log, 256 * 1024, "tail")
						.toString("utf8")
						.split("\n");
					const logTail = logLines.slice(Math.max(0, logLines.length - 30)).join("\n");
					if (logTail.trim()) {
						box.addChild(new Spacer(1));
						box.addChild(new Text(theme.fg("dim", logTail), 0, 0));
					}
				} catch {
					// The path remains visible even if a historical file was removed.
				}
			}

			if (data.image) {
				box.addChild(new Spacer(1));
				try {
					box.addChild(new Image(
						pngPreview(data.image).toString("base64"),
						"image/png",
						{ fallbackColor: (text) => theme.fg("muted", text) },
						{
							maxWidthCells: 72,
							maxHeightCells: 28,
							filename: basename(data.image),
						},
					));
				} catch {
					box.addChild(new Text(theme.fg("warning", "preview image unavailable"), 0, 0));
				}
				box.addChild(new Text(`image: ${data.image}`, 0, 0));
			}
			return box;
		},
	);

	function active(generation: number): boolean {
		return enabled
			&& generation === sessionGeneration
			&& context !== undefined
			&& sessionAbort?.signal.aborted !== true;
	}

	function currentSession(ctx: ExtensionContext): boolean {
		return enabled
			&& context !== undefined
			&& context.sessionManager.getSessionId() === ctx.sessionManager.getSessionId();
	}

	function notify(message: string, type: "info" | "warning" | "error", generation = sessionGeneration) {
		if (!active(generation)) return;
		context?.ui.notify(compactNotice(message), type);
	}

	function checkpointData(): MonitorCheckpoint {
		const pinnedBindings: Partial<Record<MonitorSource, string>> = {};
		for (const source of pinnedSources) {
			if (bindings[source]) pinnedBindings[source] = bindings[source];
		}
		return {
			schemaVersion: 6,
			clientId: monitorClientId,
			workflowAutoDiscovery,
			workflowClaim,
			recentTerminalWorkflow,
			bindings: pinnedBindings,
			pinnedSources: [...pinnedSources].sort(),
			workflowEventCursors: Object.fromEntries(
				workflowEventCursors.entries(),
			),
			rtlClosureEventCursors: Object.fromEntries(
				rtlClosureEventCursor.entries(),
			),
			laneEventCursors: Object.fromEntries(
				laneEventCursors.entries(),
			),
		};
	}

	function persistCheckpoint(generation = sessionGeneration) {
		if (!active(generation)) return;
		const data = checkpointData();
		const next = JSON.stringify(data);
		if (next === checkpointFingerprint) return;
		pi.appendEntry<CheckpointEntryData>(CHECKPOINT_ENTRY, {
			...data,
			savedAt: Date.now(),
		});
		checkpointFingerprint = next;
	}

	function restoreCheckpoint(ctx: ExtensionContext) {
		const entries = ctx.sessionManager.getBranch();
		for (let index = entries.length - 1; index >= 0; index -= 1) {
			const entry = entries[index];
			if (entry.type !== "custom" || entry.customType !== CHECKPOINT_ENTRY) continue;
			const parsed = monitorCheckpoint(entry.data);
			if (!parsed) continue;
			for (const source of parsed.pinnedSources) {
				const runId = parsed.bindings[source];
				if (runId) {
					bindings[source] = runId;
					pinnedSources.add(source);
				}
			}
			monitorClientId = parsed.clientId;
			workflowAutoDiscovery = parsed.workflowAutoDiscovery;
			workflowClaim = parsed.workflowClaim;
			recentTerminalWorkflow = parsed.recentTerminalWorkflow;
			if (
				workflowAutoDiscovery
				&& !pinnedSources.has("workflow")
				&& (workflowClaim || recentTerminalWorkflow)
			) {
				bindings.workflow = workflowClaim ?? recentTerminalWorkflow;
			}
			for (const [runId, cursor] of Object.entries(parsed.workflowEventCursors)) {
				setRecentCursor(workflowEventCursors, runId, cursor);
			}
			for (const [runId, cursor] of Object.entries(parsed.rtlClosureEventCursors)) {
				setRecentCursor(rtlClosureEventCursor, runId, cursor);
			}
			for (const [runId, cursor] of Object.entries(parsed.laneEventCursors)) {
				setRecentCursor(laneEventCursors, runId, cursor);
			}
			checkpointFingerprint = JSON.stringify(checkpointData());
			return;
		}
	}

	function setBinding(
		source: MonitorSource,
		runId: string | undefined,
		attachment: AttachmentMode = "history",
	): boolean {
		if (runId !== undefined && !monitorRunId(runId)) return false;
		if (bindings[source] === runId) {
			if (runId === undefined) healthBySourceRun.delete(sourceRunKey(source, "discovery"));
			return false;
		}
		const previous = bindings[source];
		if (runId === undefined) delete bindings[source];
		else bindings[source] = runId;
		for (const key of healthBySourceRun.keys()) {
			if (key.startsWith(`${source}:`)) healthBySourceRun.delete(key);
		}
		terminalBySource.delete(source);
		if (previous) liveAttachments.delete(sourceRunKey(source, previous));
		if (runId && attachment === "live") liveAttachments.add(sourceRunKey(source, runId));
		if (source === "workflow") workflowBindingStatus = undefined;
		if (source === "workflow" && runId && !workflowEventCursors.has(runId)) {
			setRecentCursor(
				workflowEventCursors,
				runId,
				initialWorkflowEventCursor(attachment === "history"),
			);
		}
		if (source === "rtl_closure" && runId && attachment === "live") {
			if (!rtlClosureEventCursor.has(runId)) setRecentCursor(rtlClosureEventCursor, runId, 0);
		}
		if (source === "lane" && runId) {
			if (!laneEventCursors.has(runId)) {
				setRecentCursor(
					laneEventCursors,
					runId,
					initialLaneEventCursor(attachment === "history"),
				);
			}
		}
		return true;
	}

	function claimWorkflow(runId: string, attachment: AttachmentMode): boolean {
		if (!monitorRunId(runId)) return false;
		const switched = bindings.workflow !== runId;
		workflowClaim = runId;
		workflowClaimPending = false;
		pendingWorkflowClaimPrevious = undefined;
		recentTerminalWorkflow = undefined;
		setBinding("workflow", runId, attachment);
		if (switched) {
			setBinding("rtl_closure", undefined);
			setBinding("lane", undefined);
			if (busyTerminalWorkflow !== runId) busyTerminalWorkflow = undefined;
		}
		return switched;
	}

	function childAttachment(workflowId: string): AttachmentMode {
		return liveAttachments.has(sourceRunKey("workflow", workflowId))
			|| fingerprints.has(sourceRunKey("workflow", workflowId))
			? "live"
			: "history";
	}

	function aggregateHealth(): MonitorHealth {
		const failed = [...healthBySourceRun.values()].filter((item) => item.degraded);
		return {
			degraded: failed.length > 0,
			consecutiveFailures: failed.reduce(
				(total, item) => total + item.consecutiveFailures,
				0,
			),
			error: failed.flatMap((item) => item.error ? [item.error] : []).slice(0, 3).join("; ") || undefined,
			transition: "unchanged",
		};
	}

	async function validateFollowTarget(
		source: MonitorSource,
		runId: string,
	): Promise<string | undefined> {
		if (source === "workflow") {
			const result = await orchestratorJsonCommand(
				pi,
				["status", "--run-id", runId, "--compact", "--json"],
				"run.get",
				sessionAbort?.signal,
			);
			if (result.absent) return `workflow does not exist: ${runId}`;
			if (result.error) return result.error;
			const snapshot = result.value ? workflowSnapshot(result.value) : undefined;
			return snapshot?.runId === runId ? undefined : "workflow status identity is invalid";
		}
		const command = source === "rtl_closure" ? "rtl-closure" : "lane-agent";
		const args = source === "rtl_closure"
			? ["report", "--run-id", runId, "--compact", "--json"]
			: source === "lane"
				? ["status", "--run-id", runId, "--json"]
				: [`${source}-status`, "--run-id", runId];
		const result = await jsonCommand(pi, command, args, sessionAbort?.signal);
		if (result.absent) return `${source} run does not exist: ${runId}`;
		if (result.error) return result.error;
		const snapshot = result.value
			? source === "rtl_closure"
				? rtlClosureSnapshot(result.value)
				: source === "lane"
					? laneSnapshot(result.value)
					: installSnapshot(source, result.value)
			: undefined;
		return snapshot?.runId === runId ? undefined : `${source} status identity is invalid`;
	}

	function appendStepRecord(
		snapshot: Snapshot,
		step: StepRecord,
		image: string | undefined,
		visualizationOnly: boolean,
	) {
		pi.appendEntry<StepEntryData>("pi-lane-step", {
			runId: snapshot.runId,
			ordinal: step.ordinal,
			step: step.id,
			phase: step.phase,
			status: step.status,
			directory: step.directory,
			commands: step.commands,
			log: step.logs.at(-1),
			runtime: step.runtime,
			image,
			visualizationOnly,
			timestamp: Date.now(),
		});
	}

	function recordLaneEvents(
		snapshot: Snapshot,
		events: LaneTimelineEvent[],
		announce: boolean,
		generation: number,
	) {
		let steps = laneEventSteps.get(snapshot.runId);
		if (!steps) {
			steps = new Map<number, StepRecord>();
			laneEventSteps.set(snapshot.runId, steps);
		}
		let completed = announcedSteps.get(snapshot.runId);
		if (!completed) {
			completed = new Set<number>();
			announcedSteps.set(snapshot.runId, completed);
		}
		let visualized = announcedVisualizations.get(snapshot.runId);
		if (!visualized) {
			visualized = new Set<number>();
			announcedVisualizations.set(snapshot.runId, visualized);
		}

		for (const event of events) {
			if (!active(generation)) continue;
			if (event.ordinal === undefined) {
				if (announce && [
					"run_started",
					"status_changed",
					"run_completed",
					"run_failed",
					"run_incomplete",
					"run_stopped",
					"visualization_status_changed",
				].includes(event.event)) {
					notify(
						[
							`[Pi Lane] Lane ${event.event}`,
							`run: ${snapshot.runId}`,
							event.phase ? `phase: ${event.phase}` : undefined,
							event.status ? `status: ${event.status}` : undefined,
							event.message,
							event.error ? `error: ${event.error}` : undefined,
						].flatMap((line) => line ? [line] : []).join("\n"),
						event.severity ?? (event.event === "run_failed" ? "error" : "info"),
						generation,
					);
				}
				continue;
			}
			const previous = steps.get(event.ordinal);
			const step: StepRecord = {
				ordinal: event.ordinal,
				id: event.step ?? previous?.id ?? `Step ${event.ordinal}`,
				phase: previous?.phase ?? "implementation",
				status: previous?.status ?? "running",
				directory: event.directory ?? previous?.directory ?? "",
				commands: event.commands ?? previous?.commands,
				logs: event.logs.length > 0 ? event.logs : (previous?.logs ?? []),
				runtime: previous?.runtime,
				visualizationStatus: previous?.visualizationStatus ?? "pending",
				visualization: event.image ?? previous?.visualization,
				visualizationError: event.error ?? previous?.visualizationError,
			};

			if (event.event === "step_started") {
				steps.set(event.ordinal, step);
				if (announce) {
					notify(
						`[Pi Lane] Step started\nrun: ${snapshot.runId}\nStep ${event.ordinal}: ${step.id}`,
						"info",
						generation,
					);
				}
				continue;
			}

			if (event.event === "step_completed") {
				step.status = "completed";
				steps.set(event.ordinal, step);
				if (!completed.has(event.ordinal)) {
					completed.add(event.ordinal);
					if (announce) appendStepRecord(snapshot, step, undefined, false);
				}
				continue;
			}

			if (["visualization_completed", "retry_completed"].includes(event.event)) {
				step.visualizationStatus = "completed";
				steps.set(event.ordinal, step);
				if (step.visualization && !visualized.has(event.ordinal)) {
					visualized.add(event.ordinal);
					if (announce) appendStepRecord(snapshot, step, step.visualization, true);
				}
				continue;
			}

			if (["visualization_failed", "retry_failed"].includes(event.event)) {
				step.visualizationStatus = "failed";
				steps.set(event.ordinal, step);
				if (announce) {
					notify(
						[
							"[Pi Lane] layout preview failed",
							`run: ${snapshot.runId}`,
							`Step ${event.ordinal}: ${step.id}`,
							`error: ${event.error ?? "renderer failed without structured detail"}`,
						].join("\n"),
						"warning",
						generation,
					);
				}
				continue;
			}

			steps.set(event.ordinal, step);
		}
	}

	function render(snapshot: Snapshot | undefined, generation = sessionGeneration) {
		if (!active(generation) || !context?.hasUI) return;
		const monitorHealth = aggregateHealth();
		if (!snapshot) {
			if (monitorHealth.degraded) {
				context.ui.setStatus(
					"pi-lane-monitor",
					agentBusy ? `Lane monitor: degraded | agent ${agentActivity}` : "Lane monitor: degraded",
				);
				const lines = [
					"Pi Lane monitor | degraded",
					monitorHealth.error ?? "managed-state polling failed",
				];
				if (agentBusy) lines.push(`agent: ${agentActivity}`);
				context.ui.setWidget("pi-lane-monitor", lines, { placement: "belowEditor" });
			} else if (agentBusy) {
				context.ui.setStatus("pi-lane-monitor", `Lane: agent | ${agentActivity}`);
				context.ui.setWidget("pi-lane-monitor", [
					`Pi Lane monitor | agent | ${agentActivity}`,
					"managed workflow: none active",
				], { placement: "belowEditor" });
			} else {
				context.ui.setStatus("pi-lane-monitor", "Lane monitor: idle");
				context.ui.setWidget("pi-lane-monitor", undefined);
			}
			return;
		}
		const progress = snapshot.currentStep ?? snapshot.phase;
		context.ui.setStatus(
			"pi-lane-monitor",
			monitorHealth.degraded
				? `Lane monitor: degraded | ${snapshot.status} | ${progress}`
				: `Lane: ${snapshot.status} | ${progress}`,
		);
		const lines = widgetLines(snapshot);
		if (monitorHealth.degraded) {
			lines.unshift(
				`monitor degraded | ${monitorHealth.consecutiveFailures} failed poll(s)`,
				monitorHealth.error ?? "managed-state polling failed",
			);
		}
		context.ui.setWidget("pi-lane-monitor", lines, {
			placement: "belowEditor",
		});
	}

	function suppressTerminalForAgent(snapshot: Snapshot | undefined): boolean {
		return Boolean(
			agentBusy
			&& snapshot
			&& snapshot.kind === "workflow"
			&& snapshot.runId === busyTerminalWorkflow
			&& TERMINAL.has(snapshot.status),
		);
	}

	function reportHealth(
		key: string,
		errors: string[],
		generation: number,
	) {
		if (!active(generation)) return;
		const previous = healthBySourceRun.get(key) ?? {
			degraded: false,
			consecutiveFailures: 0,
			transition: "unchanged" as const,
		};
		const next = nextMonitorHealth(previous, errors);
		healthBySourceRun.set(key, next);
		if (next.transition === "degraded") {
			notify(
				[
					"[Pi Lane] monitor source degraded",
					`source: ${key}`,
					`error: ${next.error ?? "managed-state polling failed"}`,
					"The last visible state for this source may be stale; polling will continue.",
				].join("\n"),
				"error",
				generation,
			);
		} else if (next.transition === "recovered") {
			notify(
				[
					"[Pi Lane] monitor source recovered",
					`source: ${key}`,
					`failed polls: ${previous.consecutiveFailures}`,
				].join("\n"),
				"info",
				generation,
			);
		}
		render(visible, generation);
	}

	function reportPollHealth(
		source: MonitorSource,
		runId: string | undefined,
		errors: string[],
		generation: number,
	) {
		reportHealth(sourceRunKey(source, runId ?? "discovery"), errors, generation);
	}

	function recordRtlClosureEvents(snapshot: Snapshot, generation: number): boolean {
		if (snapshot.kind !== "rtl_closure") return false;
		const previous = rtlClosureEventCursor.get(snapshot.runId);
		if (!snapshot.events?.length) {
			if (previous === undefined) setRecentCursor(rtlClosureEventCursor, snapshot.runId, 0);
			return false;
		}
		const drained = drainRtlClosureEvents(snapshot.events, previous);
		setRecentCursor(rtlClosureEventCursor, snapshot.runId, drained.cursor);
		for (const gap of drained.gaps) {
			notify(
				[
					"[Pi Lane] RTL-Closure event gap",
					`run: ${snapshot.runId}`,
					`missing sequence: ${gap.expected}-${gap.actual - 1}`,
					`first available: ${gap.actual}`,
					"Some transitions are no longer present in the bounded event window; current state remains available.",
				].join("\n"),
				"warning",
				generation,
			);
		}
		for (const event of drained.pending) {
			notify(
				eventSummary(snapshot.runId, event),
				eventNoticeType(event),
				generation,
			);
		}
		return drained.pending.length > 0 || drained.gaps.length > 0;
	}

	function observe(
		snapshot: Snapshot,
		generation: number,
		transitionAlreadyAnnounced = false,
		allowDisplay = true,
	) {
		if (!active(generation)) return;
		const key = sourceRunKey(snapshot.kind, snapshot.runId);
		const next = fingerprint(snapshot);
		const previous = fingerprints.get(key);
		fingerprints.set(key, next);
		let announced = transitionAlreadyAnnounced;

		const sameVisibleRun = visible?.kind === snapshot.kind
			&& visible.runId === snapshot.runId;
		const snapshotActive = ["created", "queued", "running"].includes(snapshot.status);
		const visibleActive = visible
			? ["created", "queued", "running"].includes(visible.status)
			: false;
		const workflowOwnsDisplay = visible?.kind === "workflow"
			&& bindings.workflow === visible.runId;
		if (
			allowDisplay
			&&
			!suppressTerminalForAgent(snapshot)
			&& (
			!visible
			|| sameVisibleRun
			|| snapshot.kind === "workflow"
			|| (
				!workflowOwnsDisplay
				&& snapshotActive
				&& (!visibleActive || SOURCE_PRIORITY[snapshot.kind] >= SOURCE_PRIORITY[visible.kind])
			)
			)
		) {
			visible = snapshot;
			render(visible);
		}
		if (previous === undefined) {
			if (snapshot.status === "running" && !transitionAlreadyAnnounced) {
				notify(summary(snapshot), statusType(snapshot), generation);
				announced = true;
			}
		} else if (previous !== next && !transitionAlreadyAnnounced) {
			notify(summary(snapshot), statusType(snapshot), generation);
			announced = true;
			if (
				allowDisplay
				&& TERMINAL.has(snapshot.status)
				&& !suppressTerminalForAgent(snapshot)
			) {
				visible = snapshot;
				render(snapshot);
			}
		}

		const heartbeatKey = `${snapshot.kind}:${snapshot.runId}`;
		const now = Date.now();
		if (snapshot.status !== "running") {
			lastHeartbeatAt.delete(heartbeatKey);
		} else if (announced || !lastHeartbeatAt.has(heartbeatKey)) {
			lastHeartbeatAt.set(heartbeatKey, now);
		} else if (heartbeatDue(
			snapshot.status,
			lastHeartbeatAt.get(heartbeatKey),
			now,
			heartbeatIntervalMs(),
		)) {
			notify(heartbeatSummary(snapshot), statusType(snapshot), generation);
			lastHeartbeatAt.set(heartbeatKey, now);
		}
	}

	function recordWorkflowEvents(
		workflowId: string,
		events: WorkflowEvent[],
		announce: boolean,
		generation: number,
	): boolean {
		if (!announce) return false;
		for (const event of events) {
			notify(
				workflowEventSummary(workflowId, event),
				workflowEventNoticeType(event),
				generation,
			);
		}
		return events.length > 0;
	}

	async function replayWorkflowEvents(
		workflowId: string,
		generation: number,
	): Promise<{ snapshot?: Snapshot; announced: boolean; error?: string }> {
		let cursor = workflowEventCursors.get(workflowId) ?? initialWorkflowEventCursor();
		let latest: Snapshot | undefined;
		let announced = false;
		for (let page = 0; page < MAX_EVENT_PAGES_PER_POLL; page += 1) {
			const args = [
				"events",
				"--run-id",
				workflowId,
				"--after-sequence",
				String(cursor.afterSequence),
				"--limit",
				String(EVENT_PAGE_SIZE),
				"--client-id",
				monitorClientId ?? "pi-lane-monitor",
				"--acknowledge",
				"--json",
			];
			const result = await orchestratorJsonCommand(
				pi,
				args,
				"run.events",
				sessionAbort?.signal,
			);
			if (!active(generation)) return { announced: false };
			if (result.error) return { announced, error: result.error };
			const report = result.value ? workflowEventReport(result.value) : undefined;
			if (!report) {
				return { announced, error: "pi-lanectl events returned an invalid workflow report" };
			}
			if (report.workflowId !== workflowId) {
				return {
					announced,
					error: `pi-lanectl events returned workflow ${report.workflowId}, expected ${workflowId}`,
				};
			}
			const first = report.events[0];
			if (
				(
					!first
					&& cursor.cursorSha256
					&& report.nextCursor.cursorSha256 !== cursor.cursorSha256
				)
				|| (
					first
					&& (
						(cursor.afterSequence === 0 && first.previousSha256 !== undefined)
						|| (
							cursor.afterSequence > 0
							&& cursor.cursorSha256
							&& first.previousSha256 !== cursor.cursorSha256
						)
					)
				)
			) {
				return { announced, error: "workflow event hash chain does not continue from the checkpoint" };
			}
			if (
				report.acknowledgedSequence === undefined
				|| report.acknowledgedSequence < report.nextCursor.afterSequence
			) {
				return { announced, error: "workflow event acknowledgement is behind the delivered cursor" };
			}
			latest = report.snapshot;
			announced = recordWorkflowEvents(
				workflowId,
				report.events,
				!cursor.suppressHistory,
				generation,
			) || announced;
			const suppressHistory = cursor.suppressHistory && report.hasMore;
			cursor = {
				...report.nextCursor,
				cursorSha256: report.nextCursor.cursorSha256 ?? cursor.cursorSha256,
				suppressHistory,
			};
			setRecentCursor(workflowEventCursors, workflowId, cursor);
			if (!report.hasMore) break;
		}
		return { snapshot: latest, announced };
	}

	async function workflowRun(
		generation: number,
	): Promise<{ snapshot?: Snapshot; announced: boolean; claimed: boolean }> {
		const pinned = pinnedSources.has("workflow");
		if (!workflowAutoDiscovery && !pinned) {
			return { announced: false, claimed: false };
		}
		let bound = bindings.workflow;
		const discover = !pinned && (
			!bound
			|| terminalBySource.get("workflow") === true
			|| (workflowBindingStatus !== undefined && ACTIONABLE.has(workflowBindingStatus))
			|| workflowClaimPending
		);
		if (discover) {
			const listing = await orchestratorListCommand(
				pi,
				["list", "--active", "--limit", "100", "--json"],
				sessionAbort?.signal,
			);
			if (!active(generation)) return { announced: false, claimed: Boolean(bound) };
			reportPollHealth(
				"workflow",
				undefined,
				listing.error ? [listing.error] : [],
				generation,
			);
			if (listing.error) return { announced: false, claimed: true };
			const discovered = workflowClaimPending
				? newlyStartedWorkflowRunId(listing.values ?? [], pendingWorkflowClaimPrevious)
				: preferredWorkflowRunId(listing.values ?? []);
			if (!discovered) {
				// Keep a valid terminal workflow attached. Its durable journal can
				// still receive a later integrity-degradation event; a newly active
				// workflow will preempt it on a subsequent discovery pass.
				if (!bound) {
					return {
						announced: false,
						claimed: workflowClaimPending,
					};
				}
			} else {
				const attachment = workflowClaimPending ? "live" : "history";
				claimWorkflow(discovered, attachment);
				bound = discovered;
			}
		}
		if (!bound) return { announced: false, claimed: false };
		const args = ["status", "--run-id", bound, "--compact", "--json"];
		const result = await orchestratorJsonCommand(
			pi,
			args,
			"run.get",
			sessionAbort?.signal,
		);
		if (!active(generation)) return { announced: false, claimed: Boolean(bound) };
		if (result.error) {
			reportPollHealth(
				"workflow",
				bound,
				[result.error],
				generation,
			);
			return { announced: false, claimed: Boolean(bound) };
		}
		if (result.absent) {
			if (pinned) {
				reportPollHealth(
					"workflow",
					bound,
					[`pinned workflow target is unavailable: ${bound}`],
					generation,
				);
				return { announced: false, claimed: true };
			}
			reportPollHealth("workflow", bound, [], generation);
			if (recentTerminalWorkflow === bound) recentTerminalWorkflow = undefined;
			if (workflowClaim === bound) workflowClaim = undefined;
			setBinding("workflow", undefined);
			setBinding("rtl_closure", undefined);
			setBinding("lane", undefined);
			return { announced: false, claimed: false };
		}
		const status = result.value ? workflowSnapshot(result.value) : undefined;
		if (!status) {
			if (result.value) {
				reportPollHealth(
					"workflow",
					bound,
					["workflow status omitted a valid workflow_id/status/phase/revision snapshot"],
					generation,
				);
			}
			return { announced: false, claimed: Boolean(bound) };
		}
		if (status.runId !== bound) {
			reportPollHealth(
				"workflow",
				bound,
				[`workflow status returned ${status.runId}, expected ${bound}`],
				generation,
			);
			return { announced: false, claimed: true };
		}
		setBinding("workflow", status.runId);
		if (!workflowClaim) workflowClaim = status.runId;
		const attachment = childAttachment(status.runId);
		const replay = await replayWorkflowEvents(status.runId, generation);
		if (!active(generation)) return { announced: false, claimed: true };
		reportPollHealth(
			"workflow",
			status.runId,
			replay.error ? [replay.error] : [],
			generation,
		);
		const snapshot = replay.snapshot ?? status;
		setBinding("rtl_closure", snapshot.rtlClosureRunId, attachment);
		setBinding("lane", snapshot.laneRunId, attachment);
		workflowBindingStatus = snapshot.status;
		terminalBySource.set("workflow", TERMINAL.has(snapshot.status));
		if (!pinned) {
			recentTerminalWorkflow = TERMINAL.has(snapshot.status)
				? snapshot.runId
				: undefined;
		}
		persistCheckpoint(generation);
		return {
			snapshot,
			announced: replay.announced,
			claimed: true,
		};
	}

	async function rtlClosureRun(
		generation: number,
		workflowClaimed: boolean,
	): Promise<Snapshot | undefined> {
		let bound = bindings.rtl_closure;
		const pinned = pinnedSources.has("rtl_closure");
		if (workflowClaimed && !bound) return undefined;
		if (!workflowClaimed && !pinned) return undefined;
		if (!bound) return undefined;
		const args = ["report", "--run-id", bound, "--compact", "--json"];
		const result = await jsonCommand(
			pi,
			"rtl-closure",
			args,
			sessionAbort?.signal,
		);
		if (!active(generation)) return undefined;
		reportPollHealth("rtl_closure", bound, result.error ? [result.error] : [], generation);
		if (result.absent) {
			if (pinned) {
				reportPollHealth(
					"rtl_closure",
					bound,
					[`pinned RTL-Closure target is unavailable: ${bound}`],
					generation,
				);
				return undefined;
			}
			setBinding("rtl_closure", undefined);
			return undefined;
		}
		const snapshot = result.value ? rtlClosureSnapshot(result.value) : undefined;
		if (!snapshot) {
			if (result.value) {
				reportPollHealth(
					"rtl_closure",
					bound,
					["RTL-Closure report omitted a valid run_id/status snapshot"],
					generation,
				);
			}
			return undefined;
		}
		if (snapshot.runId !== bound) {
			reportPollHealth(
				"rtl_closure",
				bound,
				[`RTL-Closure report returned run ${snapshot.runId}, expected ${bound}`],
				generation,
			);
			return undefined;
		}
		const changed = setBinding("rtl_closure", snapshot.runId);
		if (changed && !snapshot.laneRunId) setBinding("lane", undefined);
		terminalBySource.set("rtl_closure", TERMINAL.has(snapshot.status));
		return snapshot;
	}

	function laneEventArguments(runId: string, cursor: LaneEventCursor): string[] {
		const args = [
			"events",
			"--run-id",
			runId,
			"--after-sequence",
			String(cursor.afterSequence),
			"--limit",
			String(EVENT_PAGE_SIZE),
		];
		if (cursor.generation) {
			args.push("--generation", cursor.generation, "--cursor-sha256", cursor.cursorSha256);
		}
		return args;
	}

	async function replayLaneEvents(
		runId: string,
		generation: number,
	): Promise<{ snapshot?: Snapshot; error?: string; absent?: boolean }> {
		let cursor = laneEventCursors.get(runId) ?? initialLaneEventCursor();
		let latest: Snapshot | undefined;
		for (let page = 0; page < MAX_EVENT_PAGES_PER_POLL; page += 1) {
			const result = await jsonCommand(
				pi,
				"lane-agent",
				laneEventArguments(runId, cursor),
				sessionAbort?.signal,
			);
			if (!active(generation)) return {};
			if (result.error) {
				return { error: result.error };
			}
			if (result.absent) return { absent: true };
			const report = result.value ? laneEventReport(result.value, cursor) : undefined;
			if (!report) return { error: "lane-agent events returned an invalid durable-event report" };
			if (report.runId !== runId) {
				return {
					error: `lane-agent events returned run ${report.runId}, expected ${runId}`,
				};
			}
			latest = report.snapshot;
			recordLaneEvents(report.snapshot, report.events, !cursor.suppressHistory, generation);
			const suppressHistory = cursor.suppressHistory && report.hasMore;
			cursor = { ...report.nextCursor, suppressHistory };
			setRecentCursor(laneEventCursors, runId, cursor);
			if (report.invalidLineCount > 0 && !suppressHistory) {
				notify(
					`[Pi Lane] ignored ${report.invalidLineCount} invalid Lane timeline record(s)\nrun: ${runId}`,
					"warning",
					generation,
				);
			}
			if (!report.hasMore) break;
		}
		return { snapshot: latest };
	}

	async function laneRun(
		rtlClosure: Snapshot | undefined,
		generation: number,
		workflowClaimed: boolean,
	): Promise<Snapshot | undefined> {
		if (!workflowClaimed && rtlClosure?.laneRunId) {
			const attachment = liveAttachments.has(sourceRunKey("rtl_closure", rtlClosure.runId))
				|| fingerprints.has(sourceRunKey("rtl_closure", rtlClosure.runId))
				? "live"
				: "history";
			setBinding("lane", rtlClosure.laneRunId, attachment);
		}
		if (workflowClaimed && !bindings.lane) return undefined;
		if (!workflowClaimed && rtlClosure && !rtlClosure.laneRunId) return undefined;

		let runId = bindings.lane;
		const pinned = pinnedSources.has("lane");
		const childRun = workflowClaimed ? runId : rtlClosure?.laneRunId;
		if (!workflowClaimed && !childRun && !pinned) return undefined;
		if (!runId) return undefined;

		const replay = await replayLaneEvents(runId, generation);
		if (!active(generation)) return undefined;
		const statusResult = await jsonCommand(
			pi,
			"lane-agent",
			["status", "--run-id", runId, "--json"],
			sessionAbort?.signal,
		);
		if (!active(generation)) return undefined;
		if (statusResult.absent || replay.absent) {
			if (pinned || workflowClaimed) {
				reportPollHealth(
					"lane",
					runId,
					[`${pinned ? "pinned" : "workflow-owned"} Lane target is unavailable: ${runId}`],
					generation,
				);
			} else {
				setBinding("lane", undefined);
				laneEventSteps.delete(runId);
			}
			return undefined;
		}
		reportPollHealth(
			"lane",
			runId,
			[replay.error, statusResult.error].flatMap((item) => item ? [item] : []),
			generation,
		);
		const status = statusResult.value ? laneSnapshot(statusResult.value) : undefined;
		if (statusResult.value && !status) {
			reportPollHealth(
				"lane",
				runId,
				["lane status omitted a valid run_id/status snapshot"],
				generation,
			);
		}
		if (status && status.runId !== runId) {
			reportPollHealth(
				"lane",
				runId,
				[`lane status returned run ${status.runId}, expected ${runId}`],
				generation,
			);
			return undefined;
		}
		const snapshot = status ?? replay.snapshot;
		if (!snapshot) return undefined;
		const eventSteps = laneEventSteps.get(runId);
		if (eventSteps && eventSteps.size > 0) {
			const merged = new Map(
				(snapshot.stepHistory ?? []).map((step) => [step.ordinal, step] as const),
			);
			for (const [ordinal, step] of eventSteps) {
				const baseline = merged.get(ordinal);
				if (!baseline) {
					merged.set(ordinal, step);
					continue;
				}
				merged.set(ordinal, {
					...baseline,
					...step,
					phase: step.phase === "implementation" ? baseline.phase : step.phase,
					status: baseline.status === "completed" ? "completed" : step.status,
					directory: step.directory || baseline.directory,
					commands: step.commands ?? baseline.commands,
					logs: step.logs.length > 0 ? step.logs : baseline.logs,
					runtime: step.runtime ?? baseline.runtime,
					visualizationStatus: step.visualizationStatus === "pending"
						? baseline.visualizationStatus
						: step.visualizationStatus,
					visualization: step.visualization ?? baseline.visualization,
					visualizationError: step.visualizationError ?? baseline.visualizationError,
				});
			}
			snapshot.stepHistory = [...merged.values()].sort(
				(left, right) => left.ordinal - right.ordinal,
			);
		}
		latestLane = snapshot;
		terminalBySource.set("lane", TERMINAL.has(snapshot.status));
		return snapshot;
	}

	async function installRun(
		source: "pdk" | "image",
		generation: number,
	): Promise<Snapshot | undefined> {
		const bound = bindings[source];
		const pinned = pinnedSources.has(source);
		const discover = !bound || (!pinned && terminalBySource.get(source) === true);
		const args = [`${source}-status`];
		if (!discover && bound) args.push("--run-id", bound);
		const result = await jsonCommand(pi, "lane-agent", args, sessionAbort?.signal);
		if (!active(generation)) return undefined;
		reportPollHealth(source, discover ? undefined : bound, result.error ? [result.error] : [], generation);
		if (result.absent) {
			if (pinned && bound) {
				reportPollHealth(
					source,
					bound,
					[`pinned ${source} install target is unavailable: ${bound}`],
					generation,
				);
				return undefined;
			}
			if (!discover) setBinding(source, undefined);
			return undefined;
		}
		const snapshot = result.value ? installSnapshot(source, result.value) : undefined;
		if (!snapshot) {
			if (result.value) {
				reportPollHealth(
					source,
					discover ? undefined : bound,
					[`${source} status omitted a valid run_id/status snapshot`],
					generation,
				);
			}
			return undefined;
		}
		if (!discover && snapshot.runId !== bound) {
			reportPollHealth(
				source,
				bound,
				[`${source} status returned run ${snapshot.runId}, expected ${bound}`],
				generation,
			);
			return undefined;
		}
		if (discover && !pinned && TERMINAL.has(snapshot.status)) {
			if (bound) setBinding(source, undefined);
			return undefined;
		}
		setBinding(source, snapshot.runId);
		terminalBySource.set(source, TERMINAL.has(snapshot.status));
		return snapshot;
	}

	async function pollOnce(generation: number) {
		if (!active(generation)) return;
		try {
			const workflow = await workflowRun(generation);
			if (!active(generation)) return;
			const rtlClosure = await rtlClosureRun(generation, workflow.claimed);
			if (!active(generation)) return;
			const lane = await laneRun(rtlClosure, generation, workflow.claimed);
			if (!active(generation)) return;

			if (rtlClosure && lane && lane.runId === rtlClosure.laneRunId) {
				rtlClosure.visualizationStatus = lane.visualizationStatus;
				rtlClosure.visualizationCount = lane.visualizationCount;
				rtlClosure.latestVisualization = lane.latestVisualization;
				rtlClosure.visualizationErrors = lane.visualizationErrors;
			}
			if (rtlClosure) {
				observe(
					rtlClosure,
					generation,
					recordRtlClosureEvents(rtlClosure, generation),
					!workflow.claimed,
				);
			}
			if (lane && lane.runId !== rtlClosure?.laneRunId) {
				observe(lane, generation, false, !workflow.claimed);
			}
			if (workflow.snapshot) {
				observe(workflow.snapshot, generation, workflow.announced);
			}

			const managedExecution = workflow.claimed
				|| rtlClosure !== undefined
				|| lane !== undefined;
			const shouldPollInstall = (source: "pdk" | "image") => (
				pinnedSources.has(source)
				|| bindings[source] !== undefined
				|| !managedExecution
			);
			for (const source of ["pdk", "image"] as const) {
				if (shouldPollInstall(source)) continue;
				for (const key of [...healthBySourceRun.keys()]) {
					if (key.startsWith(`${source}:`)) healthBySourceRun.delete(key);
				}
			}
			const [pdk, image] = await Promise.all([
				shouldPollInstall("pdk") ? installRun("pdk", generation) : undefined,
				shouldPollInstall("image") ? installRun("image", generation) : undefined,
			]);
			if (!active(generation)) return;
			if (pdk) observe(pdk, generation);
			if (image) observe(image, generation);
			const retainedWorkflow = workflow.claimed
				&& visible?.kind === "workflow"
				&& visible.runId === bindings.workflow
				? visible
				: undefined;
			const nextVisible = workflow.claimed
				? workflow.snapshot ?? retainedWorkflow
				: rtlClosure ?? lane ?? pdk ?? image;
			if (suppressTerminalForAgent(nextVisible)) {
				visible = undefined;
			} else if (nextVisible) {
				visible = nextVisible;
			} else if (workflow.claimed && visible?.kind !== "workflow") {
				visible = undefined;
			} else if (!aggregateHealth().degraded) {
				visible = undefined;
			}
			persistCheckpoint(generation);
			reportHealth("monitor:poll", [], generation);
			render(visible, generation);
		} catch (error) {
			if (active(generation)) {
				reportHealth(
					"monitor:poll",
					[`monitor internal polling error: ${errorMessage(error)}`],
					generation,
				);
			}
		}
	}

	function poll(generation = sessionGeneration): Promise<void> {
		if (!active(generation)) return Promise.resolve();
		if (activePoll?.generation === generation) return activePoll.promise;
		const promise = pollOnce(generation).finally(() => {
			if (activePoll?.promise === promise) activePoll = undefined;
		});
		activePoll = { generation, promise };
		return promise;
	}

	function clearSessionState() {
		fingerprints.clear();
		announcedSteps.clear();
		announcedVisualizations.clear();
		workflowEventCursors.clear();
		rtlClosureEventCursor.clear();
		laneEventCursors.clear();
		laneEventSteps.clear();
		liveAttachments.clear();
		terminalBySource.clear();
		healthBySourceRun.clear();
		lastHeartbeatAt.clear();
		activeAgentTools.clear();
		workflowToolIntents.clear();
		agentBusy = false;
		agentActivity = "processing request";
		busyTerminalWorkflow = undefined;
		pinnedSources.clear();
		for (const source of SOURCES) delete bindings[source];
		visible = undefined;
		latestLane = undefined;
		monitorClientId = undefined;
		workflowAutoDiscovery = true;
		workflowClaim = undefined;
		workflowClaimPending = false;
		pendingWorkflowClaimPrevious = undefined;
		workflowBindingStatus = undefined;
		recentTerminalWorkflow = undefined;
		checkpointFingerprint = "";
	}

	function start(ctx: ExtensionContext) {
		if (timer) clearInterval(timer);
		sessionAbort?.abort();
		if (context && context !== ctx) {
			context.ui.setStatus("pi-lane-monitor", undefined);
			context.ui.setWidget("pi-lane-monitor", undefined);
		}
		sessionGeneration += 1;
		const generation = sessionGeneration;
		context = ctx;
		sessionAbort = new AbortController();
		clearSessionState();
		restoreCheckpoint(ctx);
		const sessionClientId = `pi-lane-monitor:${ctx.sessionManager.getSessionId()}`;
		if (monitorClientId !== sessionClientId) monitorClientId = sessionClientId;
		render(undefined, generation);
		void poll(generation);
		timer = setInterval(() => void poll(generation), intervalMs());
		timer.unref?.();
	}

	function restartPolling(ctx: ExtensionContext) {
		if (timer) clearInterval(timer);
		sessionAbort?.abort();
		sessionGeneration += 1;
		const generation = sessionGeneration;
		context = ctx;
		sessionAbort = new AbortController();
		render(visible, generation);
		void poll(generation);
		timer = setInterval(() => void poll(generation), intervalMs());
		timer.unref?.();
	}

	function stop() {
		if (timer) clearInterval(timer);
		timer = undefined;
		sessionAbort?.abort();
		sessionAbort = undefined;
		sessionGeneration += 1;
		activePoll = undefined;
		context?.ui.setStatus("pi-lane-monitor", undefined);
		context?.ui.setWidget("pi-lane-monitor", undefined);
		context = undefined;
	}

	pi.on("session_start", async (_event, ctx) => start(ctx));
	pi.on("session_shutdown", async () => stop());
	pi.on("agent_start", async (_event, ctx) => {
		if (!currentSession(ctx)) return;
		agentBusy = true;
		agentActivity = "processing request";
		busyTerminalWorkflow = visible?.kind === "workflow" && TERMINAL.has(visible.status)
			? visible.runId
			: terminalBySource.get("workflow") === true
				? bindings.workflow
				: undefined;
		if (visible && TERMINAL.has(visible.status) && !pinnedSources.has(visible.kind)) {
			visible = undefined;
		}
		render(visible);
	});
	pi.on("tool_execution_start", async (event, ctx) => {
		if (!currentSession(ctx)) return;
		const toolName = event.toolName.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 64) || "tool";
		const intent = workflowToolIntent(toolName, event.args);
		if (intent) workflowToolIntents.set(event.toolCallId, intent);
		activeAgentTools.set(event.toolCallId, toolName);
		agentBusy = true;
		agentActivity = activeAgentTools.size > 1
			? `tools ${activeAgentTools.size} active`
			: `tool ${toolName}`;
		render(visible);
	});
	pi.on("tool_execution_end", async (event, ctx) => {
		if (!currentSession(ctx)) return;
		const intent = workflowToolIntents.get(event.toolCallId);
		workflowToolIntents.delete(event.toolCallId);
		if (intent && !event.isError) {
			const runId = intent.runId ?? workflowIdFromToolResult(event.result);
			if (runId) {
				workflowAutoDiscovery = true;
				for (const source of ["workflow", "rtl_closure", "lane"] as const) {
					pinnedSources.delete(source);
				}
				claimWorkflow(runId, "live");
				restartPolling(ctx);
				persistCheckpoint(sessionGeneration);
			} else if (intent.action === "start") {
				workflowClaimPending = true;
				pendingWorkflowClaimPrevious = bindings.workflow;
				restartPolling(ctx);
			}
		}
		activeAgentTools.delete(event.toolCallId);
		agentActivity = activeAgentTools.size > 0
			? `tools ${activeAgentTools.size} active`
			: "processing request";
		render(visible);
	});
	pi.on("agent_settled", async (_event, ctx) => {
		if (!currentSession(ctx)) return;
		activeAgentTools.clear();
		agentBusy = false;
		agentActivity = "processing request";
		busyTerminalWorkflow = undefined;
		render(visible);
	});

	pi.registerCommand("lane-monitor", {
		description: "Control Pi Lane reporting or show the latest layout preview",
		handler: async (args, ctx) => {
			const tokens = args.trim().split(/\s+/).filter(Boolean);
			const action = tokens[0]?.toLowerCase() || "status";
			if (action === "on") {
				enabled = true;
				start(ctx);
				ctx.ui.notify("Pi Lane monitor enabled", "info");
				return;
			}
			if (action === "off") {
				enabled = false;
				stop();
				ctx.ui.notify("Pi Lane monitor disabled", "warning");
				return;
			}
			if (action === "status") {
				await poll(sessionGeneration);
				const monitorHealth = aggregateHealth();
				if (monitorHealth.degraded) {
					ctx.ui.notify(
						compactNotice(`Pi Lane monitor degraded: ${monitorHealth.error ?? "managed-state polling failed"}`),
						"error",
					);
					return;
				}
				ctx.ui.notify(
						compactNotice(visible
							? summary(visible)
							: agentBusy
								? `Pi Lane agent: ${agentActivity}; no managed workflow is active yet`
								: "Pi Lane monitor: no managed run found"),
					visible ? statusType(visible) : "info",
				);
				return;
			}
			if (action === "latest") {
				await poll(sessionGeneration);
				const step = [...(latestLane?.stepHistory ?? [])]
					.reverse()
					.find((item) => item.visualization);
				if (latestLane && step?.visualization) {
					appendStepRecord(latestLane, step, step.visualization, true);
					return;
				}
				ctx.ui.notify("Pi Lane monitor: no layout preview available", "warning");
				return;
			}
			if (action === "follow") {
				const source = tokens[1]?.toLowerCase() as MonitorSource | undefined;
				const runId = monitorRunId(tokens[2]);
				if (!source || !SOURCES.includes(source) || !runId) {
					ctx.ui.notify(
						"Usage: /lane-monitor follow <workflow|rtl_closure|lane|pdk|image> <run-id>",
						"error",
					);
					return;
				}
				const validationError = await validateFollowTarget(source, runId);
				if (validationError) {
					ctx.ui.notify(
						compactNotice(`Pi Lane monitor cannot follow ${source}:${runId}\n${validationError}`),
						"error",
					);
					return;
				}
				pinnedSources.add(source);
				setBinding(source, runId);
					if (source === "workflow") {
						workflowClaim = runId;
						workflowClaimPending = false;
						pendingWorkflowClaimPrevious = undefined;
						recentTerminalWorkflow = undefined;
					workflowAutoDiscovery = true;
					pinnedSources.delete("rtl_closure");
					pinnedSources.delete("lane");
					setBinding("rtl_closure", undefined);
					setBinding("lane", undefined);
					} else if (source === "rtl_closure") {
						workflowClaim = undefined;
						workflowClaimPending = false;
						pendingWorkflowClaimPrevious = undefined;
						recentTerminalWorkflow = undefined;
					workflowAutoDiscovery = false;
					pinnedSources.delete("workflow");
					pinnedSources.delete("lane");
					for (const key of healthBySourceRun.keys()) {
						if (key.startsWith("workflow:")) healthBySourceRun.delete(key);
					}
					setBinding("workflow", undefined);
					setBinding("lane", undefined);
					} else if (source === "lane") {
						workflowClaim = undefined;
						workflowClaimPending = false;
						pendingWorkflowClaimPrevious = undefined;
						recentTerminalWorkflow = undefined;
					workflowAutoDiscovery = false;
					pinnedSources.delete("workflow");
					pinnedSources.delete("rtl_closure");
					for (const key of healthBySourceRun.keys()) {
						if (key.startsWith("workflow:")) healthBySourceRun.delete(key);
					}
					setBinding("workflow", undefined);
					setBinding("rtl_closure", undefined);
				}
				restartPolling(ctx);
				persistCheckpoint(sessionGeneration);
				ctx.ui.notify(`Pi Lane monitor now follows ${source}:${runId}`, "info");
				return;
			}
			if (action === "auto") {
				const requested = tokens[1]?.toLowerCase();
				if (requested === "rtl_closure" || requested === "lane") {
					ctx.ui.notify(
						"Standalone RTL-Closure and Lane auto-discovery is disabled; use workflow auto-discovery or an explicit follow target",
						"error",
					);
					return;
				}
				const selected = requested && requested !== "all"
					? [requested as MonitorSource]
					: SOURCES;
				if (selected.some((source) => !SOURCES.includes(source))) {
					ctx.ui.notify(
						"Usage: /lane-monitor auto [workflow|pdk|image|all]",
						"error",
					);
					return;
				}
				for (const source of selected) {
					pinnedSources.delete(source);
					setBinding(source, undefined);
				}
					if (selected.includes("workflow")) {
						workflowClaim = undefined;
						workflowClaimPending = false;
						pendingWorkflowClaimPrevious = undefined;
						recentTerminalWorkflow = undefined;
					workflowAutoDiscovery = true;
					pinnedSources.delete("rtl_closure");
					pinnedSources.delete("lane");
					setBinding("rtl_closure", undefined);
					setBinding("lane", undefined);
				}
				restartPolling(ctx);
				persistCheckpoint(sessionGeneration);
				ctx.ui.notify(`Pi Lane monitor auto-discovery enabled for ${requested ?? "all sources"}`, "info");
				return;
			}
			ctx.ui.notify(
				"Usage: /lane-monitor [on|off|status|latest|follow <workflow|rtl_closure|lane|pdk|image> <run-id>|auto [workflow|pdk|image|all]]",
				"error",
			);
		},
	});
}
