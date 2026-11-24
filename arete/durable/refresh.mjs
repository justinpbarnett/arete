import { fstatSync, readSync, writeSync } from "node:fs";

export function observeRefresh(store) {
    if (process.env.ARETE_PI_REFRESH_FD !== undefined) {
        const descriptor = Number(process.env.ARETE_PI_REFRESH_FD);
        const acknowledgement = Number(process.env.ARETE_PI_REFRESH_ACK_FD);
        const selected = process.env.ARETE_PI_REFRESH_PROVIDER;
        if (!selected || [descriptor, acknowledgement].some(fd => !Number.isSafeInteger(fd) || fd < 3
            || !fstatSync(fd).isFIFO()) || descriptor === acknowledgement) {
            throw new Error("Arete's refresh boundary requires a private descriptor and provider");
        }

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
    }
}
