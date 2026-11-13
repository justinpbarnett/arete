import { existsSync, fstatSync, lstatSync, readdirSync, readlinkSync, readSync, realpathSync, statSync, writeSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

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
        if (error.code !== "ENOENT" && error.code !== "ENOTDIR") throw error;
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
    if (!metadata || !Array.isArray(metadata.roots) || !metadata.roots.length
        || [metadata.files, metadata.write_roots, metadata.write_files].some(paths => !Array.isArray(paths))
        || [...metadata.roots, ...metadata.files, ...metadata.write_roots, ...metadata.write_files]
            .some(path => typeof path !== "string" || !isAbsolute(path))) {
        throw new Error("Arete's credential boundary requires absolute protected paths");
    }
    if (process.env.PI_CODING_AGENT_DIR) {
        const agent = resolve(process.env.PI_CODING_AGENT_DIR);
        const auth = join(agent, "auth.json");
        metadata.roots.push(agent);
        metadata.files.push(auth, auth + ".lock", canonical(auth), canonical(auth) + ".lock");
    }
    const inodes = new Set();
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
    metadata.files.forEach(path => remember(path));
    rememberWrites();
    if (process.env.ARETE_PI_REFRESH_FD !== undefined) {
        const descriptor = Number(process.env.ARETE_PI_REFRESH_FD);
        const acknowledgement = Number(process.env.ARETE_PI_REFRESH_ACK_FD);
        const selected = process.env.ARETE_PI_REFRESH_PROVIDER;
        if (!selected || [descriptor, acknowledgement].some(fd => !Number.isSafeInteger(fd) || fd < 3
            || !fstatSync(fd).isFIFO()) || descriptor === acknowledgement) {
            throw new Error("Arete's refresh boundary requires a private descriptor and provider");
        }
        const tracked = new WeakSet();
        pi.on("session_start", (_event, ctx) => {
            const store = ctx.modelRegistry.runtime.credentials.store;
            if (tracked.has(store)) return;
            tracked.add(store);
            const modify = store.modify.bind(store);
            store.modify = async (provider, fn, options) => {
                let before, after;
                const saved = await modify(provider, async current => {
                    before = structuredClone(current);
                    after = await fn(current);
                    return after;
                }, options);
                if (provider === selected && before?.type === "oauth" && after?.type === "oauth") {
                    writeSync(descriptor, JSON.stringify({ provider, before, after }) + "\n");
                    const reply = Buffer.alloc(1);
                    if (readSync(acknowledgement, reply, 0, 1, null) !== 1 || reply.toString() !== "1") {
                        throw new Error("Arete's private refresh channel closed");
                    }
                }
                return saved;
            };
        });
    }
    pi.on("tool_call", (event, ctx) => {
        if (event.toolName === "ask_owner") return undefined;
        try {
            if (!tools.has(event.toolName)) return { block: true, reason };
            const writing = event.toolName === "edit" || event.toolName === "write";
            const directories = [...metadata.roots, ...(writing ? metadata.write_roots : [])];
            const files = [...metadata.files, ...(writing ? metadata.write_files : [])];
            const roots = [...directories, ...files].flatMap(path => [resolve(path), canonical(path)]);
            const directoryInodes = new Set(directories.map(info).filter(stat => stat?.isDirectory()).map(identity));
            metadata.files.forEach(path => remember(path));
            if (writing) rememberWrites();
            const blocked = (path, stat = info(path)) => {
                const real = canonical(path);
                if (roots.some(root => within(root, path) || within(root, real))) return true;
                for (let parent = dirname(real);; parent = dirname(parent)) {
                    const ancestor = info(parent);
                    if (ancestor && directoryInodes.has(identity(ancestor))) return true;
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
                if (roots.some(root => within(path, root) || within(real, root))) return { block: true, reason };
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
