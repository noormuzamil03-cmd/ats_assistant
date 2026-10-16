// ATS Assistant: floating chat widget for the Frappe desk
(function () {
	const API = "ats_assistant.assistant.api.ask";
	const STT_URL = "/api/method/ats_assistant.assistant.voice.transcribe";
	const TTS_API = "ats_assistant.assistant.voice.speak";
	const MAX_KEEP = 30;
	const MAX_RECORD_SECS = 60;

	function store_key() {
		return "ats_ai_chat_" + ((window.frappe && frappe.session && frappe.session.user) || "guest");
	}
	function load() {
		try { return JSON.parse(localStorage.getItem(store_key()) || "[]"); } catch (e) { return []; }
	}
	function save(list) {
		try { localStorage.setItem(store_key(), JSON.stringify(list.slice(-MAX_KEEP))); } catch (e) { /* ignore */ }
	}
	function esc(t) {
		return String(t == null ? "" : t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
	}

	// Small, safe markdown: tables, lists, bold, code, links (only relative /app or /desk links)
	function md(text) {
		const lines = esc(text).split("\n");
		let html = "", i = 0;
		const inline = (s) => s
			.replace(/`([^`]+)`/g, "<code>$1</code>")
			.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
			.replace(/\[([^\]]+)\]\((\/[^)]*)\)/g, (m, label, url) =>
				'<a href="' + encodeURI(url.trim()).replace(/"/g, "%22") + '" class="ats-ai-link">' + label + "</a>");
		while (i < lines.length) {
			const line = lines[i];
			if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
				const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
				html += '<div class="ats-ai-tw"><table><thead><tr>' + cells(line).map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
				i += 2;
				while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
					html += "<tr>" + cells(lines[i]).map((c) => "<td>" + c + "</td>").join("") + "</tr>";
					i++;
				}
				html += "</tbody></table></div>";
				continue;
			}
			if (/^\s*[-*•]\s+/.test(line)) {
				html += "<ul>";
				while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) {
					html += "<li>" + inline(lines[i].replace(/^\s*[-*•]\s+/, "")) + "</li>";
					i++;
				}
				html += "</ul>";
				continue;
			}
			if (/^\s*\d+[.)]\s+/.test(line)) {
				html += "<ol>";
				while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
					html += "<li>" + inline(lines[i].replace(/^\s*\d+[.)]\s+/, "")) + "</li>";
					i++;
				}
				html += "</ol>";
				continue;
			}
			const h = line.match(/^\s*#{1,4}\s+(.*)$/);
			html += h ? "<p><b>" + inline(h[1]) + "</b></p>" : (line.trim() ? "<p>" + inline(line) + "</p>" : "");
			i++;
		}
		return html;
	}

	const ROLE = "AI Assistant User";

	function allowed() {
		if (!window.frappe || !frappe.session) return false;
		if (frappe.session.user === "Administrator") return true;
		return (frappe.user_roles || []).includes(ROLE);
	}

	const ICON = {
		chat: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 01-11.6 7.1L4 21l1.9-5.4A8 8 0 1121 12z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg>',
		send: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>',
		mic: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0014 0M12 17v5M8 22h8"/></svg>',
		stop: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
		on: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 010 7M19 5a10 10 0 010 14"/></svg>',
		off: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M22 9l-6 6M16 9l6 6"/></svg>',
		play: '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg>',
	};

	function pref(key, value) {
		const k = "ats_ai_" + key + "_" + ((frappe.session && frappe.session.user) || "guest");
		try {
			if (value === undefined) return localStorage.getItem(k);
			localStorage.setItem(k, value);
		} catch (e) { /* ignore */ }
		return value;
	}

	function server_error(data, fallback) {
		try {
			if (data && data._server_messages) {
				const msgs = JSON.parse(data._server_messages).map((m) => {
					try { return JSON.parse(m).message; } catch (e) { return m; }
				});
				if (msgs.length) return $("<div>").html(msgs.join(" ")).text();
			}
			if (data && data.exception) return String(data.exception).split(":").slice(-1)[0].trim();
		} catch (e) { /* ignore */ }
		return fallback;
	}

	// ---------------------------------------------------------------- voice output
	const Speaker = {
		audio: null,
		button: null,
		stop() {
			if (this.audio) { this.audio.pause(); this.audio = null; }
			if (window.speechSynthesis) window.speechSynthesis.cancel();
			if (this.button) { this.button.classList.remove("playing"); this.button.innerHTML = ICON.play + " Suno"; }
			this.button = null;
		},
		browser_voice(text, lang) {
			if (!window.speechSynthesis) return false;
			const voices = window.speechSynthesis.getVoices();
			const urdu = /[؀-ۿ]/.test(text);
			const pick = (re) => voices.find((v) => re.test(v.lang));
			const voice = urdu ? (pick(/^ur/i) || pick(/^hi/i)) : (pick(/^en-(IN|PK)/i) || pick(/^en/i));
			if (urdu && !voice) return false;
			const u = new SpeechSynthesisUtterance(text);
			if (voice) { u.voice = voice; u.lang = voice.lang; } else { u.lang = lang || "en-US"; }
			u.onend = () => this.stop();
			window.speechSynthesis.speak(u);
			return true;
		},
		play(text, button) {
			this.stop();
			this.button = button || null;
			if (button) { button.classList.add("playing"); button.innerHTML = ICON.stop + " Load…"; }
			frappe.call({
				method: TTS_API,
				args: { text: text },
				freeze: false,
				callback: (r) => {
					if (this.button !== (button || null)) return; // stopped meanwhile
					const d = r.message || {};
					if (button) button.innerHTML = ICON.stop + " Roko";
					if (d.audio) {
						this.audio = new Audio(d.audio);
						this.audio.onended = () => this.stop();
						this.audio.play().catch(() => {
							if (!this.browser_voice(d.spoken || text, d.lang)) this.browser_voice(text);
						});
					} else if (!this.browser_voice(d.spoken || text, d.lang) && !this.browser_voice(text)) {
						this.stop();
						frappe.show_alert({ message: "Is browser mein awaaz ki suhulat nahi hai.", indicator: "orange" });
					}
				},
				error: () => {
					if (!this.browser_voice(text)) this.stop();
				},
			});
		},
	};
	if (window.speechSynthesis) window.speechSynthesis.getVoices();

	// ---------------------------------------------------------------- voice input
	const Recorder = {
		rec: null,
		chunks: [],
		timer: null,
		started: 0,
		supported() {
			return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
		},
		mime() {
			const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
			return types.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
		},
		async start(onTick, onDone, onError) {
			try {
				const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
				const mime = this.mime();
				this.rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
				this.chunks = [];
				this.rec.ondataavailable = (e) => { if (e.data && e.data.size) this.chunks.push(e.data); };
				this.rec.onstop = () => {
					stream.getTracks().forEach((t) => t.stop());
					clearInterval(this.timer);
					const type = (this.rec && this.rec.mimeType) || mime || "audio/webm";
					const blob = new Blob(this.chunks, { type: type });
					this.rec = null;
					onDone(blob, type);
				};
				this.rec.start();
				this.started = Date.now();
				this.timer = setInterval(() => {
					const secs = Math.round((Date.now() - this.started) / 1000);
					onTick(secs);
					if (secs >= MAX_RECORD_SECS) this.stop();
				}, 500);
			} catch (e) {
				onError(e);
			}
		},
		stop() { if (this.rec && this.rec.state !== "inactive") this.rec.stop(); },
		active() { return !!this.rec; },
	};

	function upload_audio(blob, type) {
		const ext = /ogg/.test(type) ? "ogg" : /mp4/.test(type) ? "m4a" : "webm";
		const fd = new FormData();
		fd.append("audio", blob, "voice." + ext);
		return fetch(STT_URL, {
			method: "POST",
			body: fd,
			credentials: "same-origin",
			headers: { "X-Frappe-CSRF-Token": frappe.csrf_token || "", Accept: "application/json" },
		}).then((r) => r.json().then((d) => {
			if (!r.ok || d.exc_type || d.exception) throw new Error(server_error(d, "Awaaz se text nahi ban saka."));
			return ((d.message || {}).text || "").trim();
		}));
	}

	function mic_help() {
		frappe.msgprint({
			title: "Mic is page par band hai",
			indicator: "orange",
			message:
				"Browser mic sirf <b>https</b> wale page par chalne deta hai. Do mein se ek kaam karein:<br><br>" +
				"<b>1.</b> Chrome mein <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code> kholein, " +
				"box mein <code>" + frappe.utils.escape_html(window.location.origin) + "</code> likhein, " +
				"<b>Enabled</b> karein aur Chrome <b>Relaunch</b> karein.<br>" +
				"<b>2.</b> Ya ERP ko https (SSL) par chalayen.<br><br>" +
				"Phir mic button dabane par mic ki ijazat (Allow) dein.",
		});
	}

	// ---------------------------------------------------------------- widget
	function build() {
		if (!allowed()) return;
		if (document.getElementById("ats-ai-fab")) return;
		const fab = document.createElement("button");
		fab.id = "ats-ai-fab";
		fab.title = "ATS Assistant";
		fab.innerHTML = ICON.chat;

		const panel = document.createElement("div");
		panel.id = "ats-ai-panel";
		panel.innerHTML =
			'<div class="ats-ai-head"><div><b>ATS Assistant</b><span>ERP data, reports &amp; HR</span></div>' +
			'<div><button class="ats-ai-voice" title="Jawab awaaz mein sunein (on/off)"></button>' +
			'<button class="ats-ai-clear" title="Clear chat">Clear</button><button class="ats-ai-close" title="Close">&times;</button></div></div>' +
			'<div class="ats-ai-body"></div>' +
			'<div class="ats-ai-foot">' +
			'<button class="ats-ai-mic" title="Bol kar poochein">' + ICON.mic + "</button>" +
			'<textarea rows="1" placeholder="Poochein ya mic dabayen…"></textarea>' +
			'<button class="ats-ai-send" title="Send">' + ICON.send + "</button></div>";

		document.body.appendChild(fab);
		document.body.appendChild(panel);

		const body = panel.querySelector(".ats-ai-body");
		const input = panel.querySelector("textarea");
		const send_btn = panel.querySelector(".ats-ai-send");
		const mic_btn = panel.querySelector(".ats-ai-mic");
		const voice_btn = panel.querySelector(".ats-ai-voice");
		const placeholder = input.placeholder;
		let chat = load();
		let busy = false;
		let auto_voice = pref("voice") === "1";

		function paint_voice() {
			voice_btn.innerHTML = auto_voice ? ICON.on : ICON.off;
			voice_btn.classList.toggle("on", auto_voice);
			voice_btn.title = auto_voice ? "Awaaz ON: har jawab bol kar sunaya jayega" : "Awaaz OFF";
		}
		paint_voice();

		function bubble(role, text, meta) {
			const div = document.createElement("div");
			div.className = "ats-ai-msg " + (role === "user" ? "me" : "bot");
			div.innerHTML = role === "user" ? "<p>" + esc(text) + "</p>" : md(text);
			if (role !== "user" || meta) {
				const m = document.createElement("div");
				m.className = "ats-ai-meta";
				if (meta) m.appendChild(document.createTextNode(meta));
				if (role !== "user" && text) {
					const play = document.createElement("button");
					play.className = "ats-ai-play";
					play.innerHTML = ICON.play + " Suno";
					play.addEventListener("click", () => {
						if (play.classList.contains("playing")) Speaker.stop();
						else Speaker.play(text, play);
					});
					m.appendChild(play);
				}
				div.appendChild(m);
			}
			body.appendChild(div);
			body.scrollTop = body.scrollHeight;
			return div;
		}

		function render() {
			body.innerHTML = "";
			chat.forEach((c) => bubble(c.role, c.content));
		}

		function ask(text, via_voice) {
			const q = (text || input.value).trim();
			if (!q || busy) return;
			busy = true;
			Speaker.stop();
			input.value = "";
			input.style.height = "";
			send_btn.disabled = true;
			mic_btn.disabled = true;
			const history = chat.slice(-8);
			chat.push({ role: "user", content: q });
			save(chat);
			bubble("user", q, via_voice ? "🎤" : "");
			const wait = document.createElement("div");
			wait.className = "ats-ai-msg bot ats-ai-wait";
			wait.innerHTML = "<p>…</p>";
			body.appendChild(wait);
			body.scrollTop = body.scrollHeight;
			const started = Date.now();
			const timer = setInterval(() => {
				wait.innerHTML = "<p>Soch raha hoon… " + Math.round((Date.now() - started) / 1000) + "s</p>";
			}, 1000);

			frappe.call({
				method: API,
				args: { question: q, history: JSON.stringify(history), voice: via_voice ? 1 : 0 },
				freeze: false,
				callback: (r) => {
					const d = r.message || {};
					const ans = d.answer || "Jawab nahi mila.";
					chat.push({ role: "assistant", content: ans });
					save(chat);
					wait.remove();
					const secs = Math.round((Date.now() - started) / 1000);
					const msg = bubble("assistant", ans, secs + "s");
					if ((auto_voice || via_voice) && !d.error) {
						Speaker.play(ans, msg.querySelector(".ats-ai-play"));
					}
				},
				error: () => {
					wait.remove();
					bubble("assistant", "Server se jawab nahi aaya. Dobara koshish karein.");
				},
				always: () => {
					clearInterval(timer);
					busy = false;
					send_btn.disabled = false;
					mic_btn.disabled = false;
					input.focus();
				},
			});
		}

		function stop_recording_ui() {
			mic_btn.classList.remove("rec");
			mic_btn.innerHTML = ICON.mic;
			input.placeholder = placeholder;
		}

		mic_btn.addEventListener("click", () => {
			if (busy) return;
			if (Recorder.active()) { Recorder.stop(); return; }
			if (!Recorder.supported()) { mic_help(); return; }
			Speaker.stop();
			mic_btn.classList.add("rec");
			mic_btn.innerHTML = ICON.stop;
			input.placeholder = "Bolein… (khatam ho to button dobara dabayen)";
			Recorder.start(
				(secs) => { input.placeholder = "Sun raha hoon… " + secs + "s (rokne ke liye button dabayen)"; },
				(blob, type) => {
					stop_recording_ui();
					if (blob.size < 800) return;
					input.placeholder = "Awaaz ko text mein badal raha hoon…";
					mic_btn.disabled = true;
					upload_audio(blob, type)
						.then((text) => {
							input.placeholder = placeholder;
							mic_btn.disabled = false;
							if (!text) {
								frappe.show_alert({ message: "Awaaz samajh nahi aayi, dobara bolein.", indicator: "orange" });
								return;
							}
							ask(text, true);
						})
						.catch((e) => {
							input.placeholder = placeholder;
							mic_btn.disabled = false;
							frappe.show_alert({ message: e.message || "Awaaz se text nahi ban saka.", indicator: "red" }, 7);
						});
				},
				(err) => {
					stop_recording_ui();
					if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
						frappe.show_alert({ message: "Mic ki ijazat nahi mili. Browser mein mic Allow karein.", indicator: "red" }, 7);
					} else if (err && err.name === "NotFoundError") {
						frappe.show_alert({ message: "Is computer par mic nahi mila.", indicator: "red" }, 7);
					} else {
						mic_help();
					}
				}
			);
		});

		voice_btn.addEventListener("click", () => {
			auto_voice = !auto_voice;
			pref("voice", auto_voice ? "1" : "0");
			if (!auto_voice) Speaker.stop();
			paint_voice();
		});

		fab.addEventListener("click", () => {
			panel.classList.toggle("open");
			if (panel.classList.contains("open")) {
				render();
				input.focus();
			} else {
				Speaker.stop();
				Recorder.stop();
			}
		});
		panel.querySelector(".ats-ai-close").addEventListener("click", () => {
			panel.classList.remove("open");
			Speaker.stop();
			Recorder.stop();
		});
		panel.querySelector(".ats-ai-clear").addEventListener("click", () => {
			Speaker.stop();
			chat = [];
			save(chat);
			render();
		});
		send_btn.addEventListener("click", () => ask());
		input.addEventListener("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				ask();
			}
		});
		input.addEventListener("input", () => {
			input.style.height = "";
			input.style.height = Math.min(input.scrollHeight, 120) + "px";
		});
		body.addEventListener("click", (e) => {
			const a = e.target.closest("a.ats-ai-link");
			if (!a) return;
			e.preventDefault();
			const href = a.getAttribute("href");
			if (frappe.router && frappe.router.push_state) frappe.router.push_state(href);
			else window.location.href = href;
		});
	}

	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
	else build();
	$(document).on("app_ready", build);
})();
