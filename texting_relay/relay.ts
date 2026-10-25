// Sentinel's iMessage relay on Photon Spectrum Cloud: the agent texts from a Photon line, no Mac needed.
//
// Every incoming 1:1 text goes to Sentinel (POST /texting/inbound); everything Sentinel wants to text goes out from
// the outbox (GET /texting/outbox, long polled). The relay decides nothing: who a text acts for, what the agent may
// buy and every approval live in Sentinel (sentinel/texting.py).
//
// Replies go back through the chat the person texted from: on Photon's free shared lines a line can only text a
// number that texted it first, so the relay keeps each sender's chat and answers there.
//
// Run: cd texting_relay && bun install && bun run start   (reads ../.env: SPECTRUM_PROJECT_ID, SPECTRUM_PROJECT_SECRET,
// SENTINEL_TEXTING_SECRET; SENTINEL_URL defaults to http://localhost:8000)
import { Spectrum, type Space } from 'spectrum-ts'
import { imessage } from 'spectrum-ts/providers/imessage'

const BASE = (process.env.SENTINEL_URL ?? 'http://localhost:8000').replace(/\/+$/, '')
const SECRET = process.env.SENTINEL_TEXTING_SECRET ?? ''
const projectId = process.env.SPECTRUM_PROJECT_ID ?? ''
const projectSecret = process.env.SPECTRUM_PROJECT_SECRET ?? ''
if (SECRET.length < 24 || !projectId || !projectSecret) {
    console.error('Set SPECTRUM_PROJECT_ID, SPECTRUM_PROJECT_SECRET (app.photon.codes) and SENTINEL_TEXTING_SECRET ' +
        '(the same value as in Sentinel\'s .env, 24+ characters).')
    process.exit(1)
}
const headers = { authorization: `Bearer ${SECRET}`, 'content-type': 'application/json' }

const app = await Spectrum({ projectId, projectSecret, providers: [imessage.config()] })
const chats = new Map<string, Space>() // sender handle (E.164 or email) -> their 1:1 chat with the line
const seen = new Set<string>() // message ids already forwarded, so a redelivered message is not sent twice

async function forward(from: string, text: string): Promise<void> {
    const response = await fetch(`${BASE}/texting/inbound`, {
        method: 'POST', headers, body: JSON.stringify({ from, text }),
    })
    if (response.status !== 202) console.error(`Sentinel answered ${response.status} for a text from ${from}`)
}

async function inbound(): Promise<void> {
    for await (const [space, message] of app.messages) {
        const from = message.sender?.id
        const kind = (space as Space & { type?: string }).type
        if (message.direction !== 'inbound' || message.content.type !== 'text' || !from || kind !== 'dm') continue
        if (seen.has(message.id)) continue
        seen.add(message.id)
        if (seen.size > 5000) seen.clear()
        chats.set(from, space)
        message.read().catch(() => undefined)
        try {
            await forward(from, message.content.text)
        } catch (err) {
            console.error('could not reach Sentinel:', err)
        }
    }
}

async function outbound(): Promise<void> {
    while (!stopping) {
        try {
            const response = await fetch(`${BASE}/texting/outbox?wait=25`, { headers })
            if (!response.ok) throw new Error(`outbox answered ${response.status}`)
            const { messages } = (await response.json()) as { messages: { to: string; text: string }[] }
            for (const m of messages) {
                const chat = chats.get(m.to)
                if (chat === undefined) {
                    console.error(`no open chat with ${m.to}; they need to text the line first`)
                    continue
                }
                await chat.send(m.text)
            }
        } catch (err) {
            console.error('outbox:', err)
            await new Promise((resolve) => setTimeout(resolve, 3000))
        }
    }
}

let stopping = false
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
    process.on(signal, async () => {
        stopping = true
        await app.stop()
        process.exit(0)
    })
}

console.log(`Relaying Photon iMessages to ${BASE}`)
await Promise.all([inbound(), outbound()])
