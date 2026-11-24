import { existsSync, lstatSync, readdirSync, readlinkSync, realpathSync, statSync, writeSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { observeRefresh } from "./durable/refresh.mjs";

const tools = new Set(["read", "edit", "write", "grep", "find", "ls"]);
const searches = new Set(["grep", "find", "ls"]);
const reason = "Arete protects host credentials and keeps its runtime code unchanged.";

function canonical(path, depth = 0) {
    if (depth > 64) throw new Error("Unresolvable path");
    try {
        return realpathSync(path);
    } catch (error) {
        if (error.code !== "ENOENT" && error.code !== "ENOTDIR") throw error;
        try {
            if (lstatSync(path).isSymbolicLink()) {
                return canonical(resolve(dirname(path), readlinkSync(path)), depth + 1);
            }
        } catch (missing) {
            if (missing.code !== "ENOENT" && missing.code !== "ENOTDIR") throw missing;
        }
        const parent = dirname(path);
        if (parent === path) throw error;
        return join(canonical(parent, depth + 1), basename(path));
    }
}

function inputPath(input, cwd, read) {
    let path = input.replace(/[\u00a0\u2000-\u200a\u202f\u205f\u3000]/g, " ").replace(/^@/, "");
    if (path === "~") path = homedir();
    else if (path.startsWith("~/")) path = join(homedir(), path.slice(2));
    if (path.startsWith("file://")) path = fileURLToPath(path);
    path = isAbsolute(path) ? resolve(path) : resolve(cwd, path);
    if (read && !existsSync(path)) {
        const variants = [path.replace(/ (AM|PM)\./gi, "\u202f$1."), path.normalize("NFD"),
            path.replace(/'/g, "\u2019"), path.normalize("NFD").replace(/'/g, "\u2019")];
        path = variants.find(existsSync) ?? path;
    }
    return path;
}

function within(parent, child) {
    return child === parent || child.startsWith(parent.endsWith(sep) ? parent : parent + sep);
}

function info(path) {
    try {
        return statSync(path, { bigint: true });
    } catch (error) {
        if (!["ENOENT", "ENOTDIR", "EACCES", "EPERM"].includes(error.code)) throw error;
        return undefined;
    }
}

function identity(stat) {
    return `${stat.dev}:${stat.ino}`;
}

function walk(path, visit, seen = new Set()) {
    const stat = info(path);
    if (!stat) return;
    if (visit(path, stat) === false) return;
    if (!stat.isDirectory() || seen.has(identity(stat))) return;
    seen.add(identity(stat));
    for (const name of readdirSync(path)) walk(join(path, name), visit, seen);
}

export default function (pi) {
    const metadata = JSON.parse(process.env.ARETE_PI_GUARD_PATHS ?? "null");
    if (metadata && metadata.open === undefined) metadata.open = [];
    if (!metadata || !Array.isArray(metadata.roots) || !metadata.roots.length
        || [metadata.open, metadata.files, metadata.write_roots, metadata.write_files].some(paths => !Array.isArray(paths))
        || [...metadata.open, ...metadata.roots, ...metadata.files, ...metadata.write_roots, ...metadata.write_files]
            .some(path => typeof path !== "string" || !isAbsolute(path))) {
        throw new Error("Arete's credential boundary requires absolute protected paths");
    }
    if (metadata.delegation_only) {
        const refuse = () => {
            writeSync(2, "Arete controller refused tools, ambient context or an unadmitted provider request.\n");
            process.exit(78);
        };
        let requests = 0;
        pi.on("before_agent_start", event => {
            const options = event.systemPromptOptions;
            if (!options || pi.getActiveTools().length || options.contextFiles?.length || options.skills?.length) refuse();
            writeSync(1, JSON.stringify({ type: "arete_controller_profile", tools: pi.getActiveTools(),
                context_files: options.contextFiles?.length ?? 0, skills: options.skills?.length ?? 0 }) + "\n");
        });
        pi.on("before_provider_request", event => {
            if (++requests > 1 || pi.getActiveTools().length || !event.payload
                || !Array.isArray(event.payload.tools ?? []) || event.payload.tools?.length) refuse();
        });
        pi.on("tool_call", () => ({ block: true, reason: "Controllers delegate through Arete only.", terminate: true }));
    }
    if (process.env.PI_CODING_AGENT_DIR) {
        const agent = resolve(process.env.PI_CODING_AGENT_DIR);
        const auth = join(agent, "auth.json");
        metadata.roots.push(agent);
        metadata.files.push(auth, auth + ".lock", canonical(auth), canonical(auth) + ".lock");
    }
    if (metadata.shell_tool) {
        pi.on("before_agent_start", () => {
            const shell = pi.getAllTools().find(tool => tool.name === metadata.shell_tool);
            const active = pi.getActiveTools();
            if (!active.includes(metadata.shell_tool) || active.includes("bash") || active.includes("powershell")
                || !shell?.sourceInfo?.path || resolve(shell.sourceInfo.path) !== resolve(metadata.shell_file)) {
                writeSync(2, "Arete refused an unguarded shell implementation.\n");
                process.exit(78);
            }
        });
    }
    const glob = pattern => new RegExp("^" + pattern.split("*")
        .map(part => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$");
    const secretNames = (metadata.secret_names ?? []).map(glob);
    const safeNames = (metadata.safe_names ?? []).map(glob);
    const secretDirs = new Set(metadata.secret_dirs ?? []);
    const secret = path => path.split(sep).slice(0, -1).some(part => secretDirs.has(part))
        || (secretNames.some(pattern => pattern.test(basename(path)))
            && !safeNames.some(pattern => pattern.test(basename(path))));
    const inodes = new Set(metadata.inodes ?? []);
    const writeInodes = new Set();
    const remember = (path, identities = inodes) => {
        const stat = info(path);
        if (stat && !stat.isDirectory()) identities.add(identity(stat));
    };
    const rememberWrites = () => {
        metadata.write_files.forEach(path => remember(path, writeInodes));
        for (const root of metadata.write_roots) {
            walk(root, (_path, stat) => {
                if (!stat.isDirectory()) writeInodes.add(identity(stat));
            });
        }
    };
    if (!metadata.delegation_only) {
        metadata.files.forEach(path => remember(path));
        rememberWrites();
    }
    if (process.env.ARETE_PI_REFRESH_FD !== undefined) {
        const tracked = new WeakSet();
        pi.on("session_start", (_event, ctx) => {
            const store = ctx.modelRegistry.runtime.credentials.store;
            if (!tracked.has(store)) observeRefresh(store);
            tracked.add(store);
        });
    }
    pi.on("tool_call", (event, ctx) => {
        if (event.toolName === "ask_owner" || (metadata.shell_tool && event.toolName === metadata.shell_tool)) {
            return undefined;
        }
        try {
            if (!tools.has(event.toolName)) return { block: true, reason };
            const writing = event.toolName === "edit" || event.toolName === "write";
            const directories = [...metadata.roots, ...(writing ? metadata.write_roots : [])];
            const files = [...metadata.files, ...(writing ? metadata.write_files : [])];
            const roots = directories.flatMap(path => [resolve(path), canonical(path)]);
            const protectedFiles = files.flatMap(path => [resolve(path), canonical(path)]);
            const opened = metadata.open.flatMap(path => [resolve(path), canonical(path)]);
            const depth = (paths, path) => Math.max(-1, ...paths.filter(root => within(root, path))
                .map(root => root.split(sep).length));
            const closed = path => depth(roots, path) >= 0 && depth(roots, path) >= depth(opened, path);
            const directoryInodes = new Map(directories.filter(path => info(path)?.isDirectory())
                .map(path => [identity(info(path)), canonical(path)]));
            metadata.files.forEach(path => remember(path));
            if (writing) rememberWrites();
            const blocked = (path, stat = info(path)) => {
                const real = canonical(path);
                if (metadata.workspace && (!within(resolve(metadata.workspace), path)
                    || !within(canonical(metadata.workspace), real))) return true;
                if (secret(path) || secret(real) || (stat?.isFile() && stat.nlink > 1)) return true;
                if (writing && metadata.write_roots.some(root => within(resolve(root), path)
                    || within(canonical(root), real))) return true;
                if (protectedFiles.some(file => within(file, path) || within(file, real))) return true;
                if (closed(path) || closed(real)) return true;
                for (let parent = dirname(real);; parent = dirname(parent)) {
                    const ancestor = info(parent);
                    const root = ancestor && directoryInodes.get(identity(ancestor));
                    if (root && closed(join(root, real.slice(parent.length)))) return true;
                    if (dirname(parent) === parent) break;
                }
                return stat && (inodes.has(identity(stat)) || (writing && writeInodes.has(identity(stat))));
            };
            const raw = event.input?.path ?? (searches.has(event.toolName) ? "." : null);
            if (typeof raw !== "string" || !raw) return { block: true, reason };
            const path = inputPath(raw, ctx.cwd, event.toolName === "read");
            if (blocked(path)) return { block: true, reason };
            if (searches.has(event.toolName)) {
                const real = canonical(path);
                if ([...roots, ...protectedFiles].some(root => within(path, root) || within(real, root))) {
                    return { block: true, reason };
                }
                let found = false;
                walk(path, (entry, stat) => {
                    if (blocked(entry, stat)) {
                        found = true;
                        return false;
                    }
                });
                if (found) return { block: true, reason };
            }
        } catch {
            return { block: true, reason };
        }
        return undefined;
    });
}
