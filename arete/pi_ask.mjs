import { spawnSync } from "node:child_process";

export default function (pi) {
    const tool = JSON.parse(process.env.ARETE_ASK_TOOL ?? "null");
    if (!tool || !tool.name || !Array.isArray(tool.check) || !tool.check.length) {
        throw new Error("Arete's ask tool needs ARETE_ASK_TOOL");
    }
    pi.registerTool({
        name: tool.name,
        label: tool.name,
        description: tool.description,
        parameters: tool.parameters,
        async execute(_id, params) {
            const run = spawnSync(tool.check[0], tool.check.slice(1), { input: JSON.stringify(params), encoding: "utf8" });
            const text = (run.stdout ?? "").trim() || run.error?.message || "the ask check did not run";
            if (run.status !== 0) throw new Error(text);
            return { content: [{ type: "text", text }], details: {} };
        },
    });
}
