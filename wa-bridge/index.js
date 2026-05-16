// wa-bridge: thin Node.js CLI around baileys for the Python orchestrator.
//
// Subcommands:
//   qr                       -> login via QR scan, persist creds in ./auth/
//   status                   -> JSON: { status: "logged_in" | "not_logged_in" }
//   send --phone --message [--attachment <path>] [--dry-run]
//                            -> JSON: { status: "sent"|"dry_run"|"not_on_whatsapp"|"error", ... }
//
// All structured output goes to stdout as ONE JSON object (the final line).
// Logs go to stderr.

import { default as makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from 'baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname  = path.dirname(__filename)
const AUTH_DIR   = path.join(__dirname, 'auth')

// All logger output -> stderr (so Python parses only JSON on stdout).
const logger = pino({ level: 'warn' }, pino.destination(2))

function parseArgs() {
    const args = {}
    for (let i = 3; i < process.argv.length; i++) {
        const a = process.argv[i]
        if (a.startsWith('--')) {
            const key = a.slice(2)
            const next = process.argv[i + 1]
            if (next !== undefined && !next.startsWith('--')) {
                args[key] = next
                i++
            } else {
                args[key] = true
            }
        }
    }
    return args
}

function emit(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n')
}

function fatal(msg, extra = {}) {
    emit({ status: 'error', error: String(msg), ...extra })
    process.exit(1)
}

async function makeBot() {
    if (!fs.existsSync(AUTH_DIR)) fs.mkdirSync(AUTH_DIR, { recursive: true })
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    const { version } = await fetchLatestBaileysVersion()
    const sock = makeWASocket({
        version,
        auth: state,
        logger,
        printQRInTerminal: false,
        browser: ['Outreach Agent', 'Chrome', '1.0'],
    })
    sock.ev.on('creds.update', saveCreds)
    return sock
}

function cmdQr() {
    return new Promise(async (resolve) => {
        const sock = await makeBot()
        sock.ev.on('connection.update', (u) => {
            const { qr, connection, lastDisconnect } = u
            if (qr) {
                process.stderr.write('\nOpen WhatsApp -> Settings -> Linked Devices -> Link a Device\nScan this QR:\n\n')
                qrcode.generate(qr, { small: true })
            }
            if (connection === 'open') {
                process.stderr.write('\n[OK] Logged in. Session persisted to ' + AUTH_DIR + '\n')
                emit({ status: 'logged_in' })
                try { sock.end() } catch {}
                resolve(0)
            }
            if (connection === 'close') {
                const code = lastDisconnect?.error?.output?.statusCode
                if (code === DisconnectReason.loggedOut) {
                    emit({ status: 'logged_out' })
                    resolve(1)
                } else {
                    // Auto-retry quietly
                    cmdQr().then(resolve)
                }
            }
        })
    })
}

function cmdStatus() {
    return new Promise(async (resolve) => {
        if (!fs.existsSync(path.join(AUTH_DIR, 'creds.json'))) {
            emit({ status: 'not_logged_in' })
            return resolve(1)
        }
        const sock = await makeBot()
        const t = setTimeout(() => {
            emit({ status: 'timeout' })
            try { sock.end() } catch {}
            resolve(2)
        }, 12_000)
        sock.ev.on('connection.update', (u) => {
            if (u.connection === 'open') {
                clearTimeout(t); emit({ status: 'logged_in' })
                try { sock.end() } catch {}; resolve(0)
            }
            if (u.connection === 'close') {
                clearTimeout(t)
                const code = u.lastDisconnect?.error?.output?.statusCode
                emit({ status: code === DisconnectReason.loggedOut ? 'logged_out' : 'disconnected' })
                try { sock.end() } catch {}; resolve(1)
            }
        })
    })
}

function cmdSend(args) {
    return new Promise(async (resolve) => {
        const rawPhone   = args.phone
        const message    = args.message
        const attachment = args.attachment
        const dryRun     = !!args['dry-run']

        if (!rawPhone || !message) return resolve(fatal('phone and message required'))

        const digits = String(rawPhone).replace(/\D/g, '')
        if (digits.length < 10) return resolve(fatal('invalid phone: ' + rawPhone))

        const jid = digits + '@s.whatsapp.net'

        if (attachment && !fs.existsSync(attachment)) {
            return resolve(fatal('attachment not found: ' + attachment))
        }

        const sock = await makeBot()
        let done = false

        const finish = (obj, code = 0) => {
            if (done) return
            done = true
            emit(obj)
            try { sock.end() } catch {}
            resolve(code)
        }

        sock.ev.on('connection.update', async (u) => {
            if (u.connection !== 'open') {
                if (u.connection === 'close') {
                    const code = u.lastDisconnect?.error?.output?.statusCode
                    if (code === DisconnectReason.loggedOut) {
                        finish({ status: 'not_logged_in', phone: digits, error: 'session expired — re-run qr' }, 1)
                    }
                }
                return
            }
            try {
                const checks = await sock.onWhatsApp(jid)
                const ok = Array.isArray(checks) && checks[0] && (checks[0].exists ?? true)
                if (!ok) return finish({ status: 'not_on_whatsapp', phone: digits }, 0)

                if (dryRun) return finish({
                    status: 'dry_run', phone: digits,
                    would_send_chars: message.length,
                    would_attach: attachment || null,
                }, 0)

                const target = (checks[0].jid) || jid

                let payload
                if (attachment) {
                    payload = {
                        document: { url: attachment },
                        mimetype: 'application/pdf',
                        fileName: path.basename(attachment),
                        caption: message,
                    }
                } else {
                    payload = { text: message }
                }

                await sock.sendMessage(target, payload)
                finish({ status: 'sent', phone: digits }, 0)
            } catch (e) {
                finish({ status: 'error', phone: digits, error: String(e?.message || e) }, 1)
            }
        })

        setTimeout(() => finish({ status: 'error', phone: digits, error: 'send timeout (60s)' }, 1), 60_000)
    })
}

async function main() {
    const cmd = process.argv[2]
    let code = 0
    switch (cmd) {
        case 'qr':      code = await cmdQr(); break
        case 'status':  code = await cmdStatus(); break
        case 'send':    code = await cmdSend(parseArgs()); break
        default:
            process.stderr.write('Usage: node index.js [qr|status|send ...]\n')
            code = 2
    }
    process.exit(code)
}

main().catch((e) => { process.stderr.write('FATAL: ' + (e?.stack || e) + '\n'); process.exit(99) })
