import { basename, join } from "node:path";
import { BACKGROUND_CONTEXT as context } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { AssistantEntry, createRegistry, defineDoc, defineExtension, defineSignal, defineTask, Harness } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { str } from "@earendil-works/pi-schema";
import { observeRefresh } from "./refresh.mjs";

const [directory, operation, event, provider, modelId, thinkingLevel, prompt] = process.argv.slice(2);
const objective = "Finish the approved issue. Monitor until tracker closure or PR merge.";
const goal = defineDoc({
    kind: "arete.issue", version: 1, scope: "conversation", history: "latest", fork: "initial",
    initial: () => ({ objective, task: null, state: "active" }), checkpointWhen: () => true,
});
const end = defineSignal({ name: "arete.issue.end", version: 1, schema: str() });
const monitor = defineTask({
    name: "arete.issue.monitor", version: 1, initial: () => ({ phase: "monitor" }),
    phases: {
        monitor: (_task, runtime, ctx) => runtime.commit(() => ({
            status: "sleeping", signals: [end.key("end")], checkpoint: { phase: "settle" },
        }), ctx),
        settle: async (task, runtime, ctx) => {
            const signal = await runtime.readSignal(end, "end", ctx);
            await runtime.commit(async tx => {
                (await tx.doc(goal, task.conversationId)).state = signal.value;
                return { status: "terminal", outcome: { status: "completed", result: signal.value } };
            }, ctx);
        },
    },
    abort: (_task, runtime, ctx) => runtime.commit(() => ({
        status: "terminal", outcome: { status: "aborted" },
    }), ctx),
});

let inferred = false;

async function modelAccess() {
    if (operation !== "turn") return createModels();
    const view = process.env.PI_CODING_AGENT_DIR;
    const models = await ModelRuntime.create({
        authPath: join(view, "auth.json"), modelsPath: join(view, "models.json"),
        modelsStorePath: join(view, "models-store.json"), refreshOnCreate: false,
    });
    observeRefresh(models.credentials.store);
    const stream = models.streamSimple.bind(models);
    models.streamSimple = (model, request, options) => {
        const latest = request.messages.findLast(message => message.role === "user");
        const text = typeof latest?.content === "string" ? latest.content :
            latest?.content?.filter(part => part.type === "text").map(part => part.text).join("");
        if (inferred || model.provider !== provider || model.id !== modelId || text !== prompt
            || request.tools?.length || request.messages.some(message => message.tools?.length)) {
            throw new Error("Durable inference differs from the admitted issue turn");
        }
        inferred = true;
        // Each observation already carries the current task, ledger and evidence. Keep recent dialogue in context.
        const conversation = request.messages.filter(message => ["user", "assistant"].includes(message.role));
        const system = request.messages.filter(message => message.role === "system").at(-1);
        return stream(model, { ...request, messages: [...(system ? [system] : []), ...conversation.slice(-5)] }, options);
    };
    return models;
}

const registry = createRegistry();
registry.install(defineExtension({ name: "arete", tasks: [monitor] }));
const storage = await openNodeSqliteStorage(join(directory, "issue.sqlite"));
const harness = await Harness.open(storage, { models: await modelAccess(), registry, settings: {
    stream: { maxRetries: 0, transport: "sse" }, retry: { enabled: false }, compaction: { enabled: false },
}}, context);
try {
    const root = await harness.root(context);
    const task = await root.commit(async tx => {
        const doc = await tx.doc(goal, root.id);
        if (!doc.task || (operation === "start" && doc.state !== "active")) {
            doc.task = await tx.createTask(monitor, null, { ownership: { kind: "conversation" }, background: true });
            doc.state = "active";
        }
        if (operation === "turn" && doc.state !== "active") throw new Error("The issue goal has ended");
        return doc.task;
    }, context);
    if (operation === "finish" || operation === "cancel") {
        const delivery = await harness.signal(task, end, "end", operation, context);
        if (!["delivered", "duplicate", "closed"].includes(delivery)) throw new Error("Issue signal " + delivery);
        const result = await harness.waitForTask(task, context);
        if (result.state.outcome.status !== "completed") throw new Error("Issue monitor failed to settle");
    } else if (operation === "start") {
        const graph = await harness.watchTaskGraph(context);
        try {
            if (graph.value.tasks[task]?.state.status !== "sleeping") {
                await new Promise((resolve, reject) => {
                    graph.start(call => {
                        if (call.kind !== "value") return;
                        const node = call.value.tasks[task];
                        if (node?.state.status === "sleeping") resolve();
                        else if (!node || node.blocked) reject(new Error("Issue monitor could not start"));
                    });
                    harness.resume();
                });
            }
        } finally {
            await graph.stop();
        }
    } else if (operation === "turn") {
        await root.configure({ model: { provider, modelId }, thinkingLevel, tools: [], modelTools: [],
            instructions: objective + " Use the current Arete observation and its action schema." }, context);
        const submission = await root.submit({ type: "input", requestId: event, content: prompt }, context);
        const settled = await submission.wait(context);
        if (settled.status !== "done") throw new Error("Issue decision " + (settled.reason ?? settled.status));
        const entry = await root.commit(tx => tx.entry(AssistantEntry, settled.answer), context);
        const message = inferred ? entry.model[0] : { ...entry.model[0], usage: {} };
        console.log(JSON.stringify({ type: "session", id: basename(directory) }));
        console.log(JSON.stringify({ type: "message_end", message }));
        console.log(JSON.stringify({ type: "agent_end", messages: [message], durableReplay: !inferred }));
    }
} finally {
    await harness.close(context);
}
