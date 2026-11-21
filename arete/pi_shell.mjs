import { fstatSync, readSync, writeSync } from "node:fs";

const maxReply = 262144;

export default function (pi) {
    const fd = Number(process.env.ARETE_PI_SHELL_FD);
    if (!Number.isSafeInteger(fd) || fd < 3 || !fstatSync(fd).isSocket()) {
        throw new Error("Arete's shell needs a private engine descriptor");
    }
    function run(params) {
        const request = Buffer.from(JSON.stringify(params) + "\n");
        for (let sent = 0; sent < request.length;) {
            const count = writeSync(fd, request, sent, request.length - sent);
            if (!count) throw new Error("Arete's shell channel closed");
            sent += count;
        }
        const chunks = [];
        let length = 0;
        for (;;) {
            const chunk = Buffer.alloc(8192);
            const count = readSync(fd, chunk, 0, chunk.length, null);
            if (!count || (length += count) > maxReply) throw new Error("Arete's shell channel closed or overflowed");
            chunks.push(chunk.subarray(0, count));
            if (chunk[count - 1] === 10) break;
        }
        return JSON.parse(Buffer.concat(chunks).toString());
    }
    if (process.env.ARETE_PI_OBSERVE !== undefined) {
        const command = process.env.ARETE_PI_OBSERVE;
        let excerpt;
        let submitted = false;
        const refuse = reason => {
            writeSync(2, `${reason}\n`);
            process.exit(78);
        };
        pi.on("before_agent_start", event => {
            const selected = event.systemPromptOptions?.selectedTools;
            if (excerpt !== undefined || !Array.isArray(selected) || selected.length || pi.getActiveTools().length) {
                refuse("Arete refused a repeated or tool-enabled native observation.");
            }
            const { output, error, exit_code } = run({ command });
            writeSync(1, JSON.stringify({ type: "arete_native_observation", command, output, error, exit_code }) + "\n");
            if (error || exit_code !== 0 || !output) refuse("Arete's native read-only observation failed.");
            excerpt = output;
            return { message: { customType: "arete_native_observation", display: true, content: excerpt } };
        });
        pi.on("before_provider_request", ({ payload }) => {
            const inputs = payload?.input;
            const declared = payload?.tools ?? [];
            const observations = Array.isArray(inputs) ? inputs.filter(item => item.role === "user"
                && Array.isArray(item.content) && item.content.length === 1 && item.content[0]?.type === "input_text"
                && item.content[0].text === excerpt) : [];
            if (submitted || !excerpt || pi.getActiveTools().length || !Array.isArray(declared) || declared.length
                || observations.length !== 1) {
                refuse("Arete refused a provider request without the exact tool-free native observation.");
            }
            submitted = true;
            writeSync(1, JSON.stringify({ type: "arete_native_observation_submitted" }) + "\n");
        });
        return;
    }
    pi.registerTool({
        name: "arete_shell",
        label: "Arete shell",
        executionMode: "sequential",
        description: "Run a shell command under this step's permissions, guard and filesystem confinement. "
            + "The working directory and environment are fixed by Arete. Commands stop at 120 seconds or 32768 output bytes.",
        parameters: {
            type: "object",
            properties: { command: { type: "string", minLength: 1, maxLength: 65536 },
                timeout: { type: "number", exclusiveMinimum: 0, maximum: 120 } },
            required: ["command"],
            additionalProperties: false,
        },
        async execute(_id, params, signal) {
            if (signal?.aborted) throw new Error("Shell cancelled");
            const reply = run(params);
            const text = [reply.output, reply.error, `Exit code: ${reply.exit_code}`].filter(Boolean).join("\n");
            return { content: [{ type: "text", text }], details: {},
                isError: Boolean(reply.error) || reply.exit_code !== 0 };
        },
    });
}
