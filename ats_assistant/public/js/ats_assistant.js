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
			.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>")
			.replace(/(^|[\s(])_([^_\s][^_]*?)_(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>")
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
			if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { i++; continue; } // "---" lines
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
			if (this.button) { this.button.classList.remove("playing"); this.button.innerHTML = ICON.play + " Listen"; }
			this.button = null;
		},
		browser_voice(text, lang) {
			if (!window.speechSynthesis || !text) return false;
			const voices = window.speechSynthesis.getVoices();
			const pick = (re) => voices.find((v) => re.test(v.lang));
			let voice;
			if (/^en/i.test(lang || "")) voice = pick(/^en-(US|GB)/i) || pick(/^en/i); // English answer
			else if (/[\u0600-\u06FF]/.test(text)) voice = pick(/^ur/i); // Urdu script: only an Urdu voice
			else if (/[\u0900-\u097F]/.test(text)) voice = pick(/^hi/i); // Devanagari: Hindi voice sounds like Urdu
			else voice = pick(/^ur/i) || pick(/^hi/i) || pick(/^en-(IN|PK)/i) || pick(/^en/i); // Roman Urdu
			if (!voice) return false;
			const u = new SpeechSynthesisUtterance(text.replace(/[*_`#|>]/g, " "));
			u.voice = voice;
			u.lang = voice.lang;
			u.rate = 0.95;
			u.onend = () => this.stop();
			window.speechSynthesis.speak(u);
			return true;
		},
		fallback(d, text) {
			if (!this.warned) {
				this.warned = true;
				frappe.show_alert({ message: "Server voice is not available, using the browser voice.", indicator: "orange" }, 6);
			}
			return this.browser_voice(d.spoken_hi) || this.browser_voice(d.spoken, d.lang) || this.browser_voice(text, d.lang);
		},
		play(text, button) {
			this.stop();
			this.button = button || null;
			if (button) { button.classList.add("playing"); button.innerHTML = ICON.stop + " Loading…"; }
			frappe.call({
				method: TTS_API,
				args: { text: text },
				freeze: false,
				callback: (r) => {
					if (this.button !== (button || null)) return; // stopped meanwhile
					const d = r.message || {};
					if (button) button.innerHTML = ICON.stop + " Stop";
					if (d.audio) {
						this.audio = new Audio(d.audio);
						this.audio.onended = () => this.stop();
						this.audio.play().catch(() => { if (!this.fallback(d, text)) this.stop(); });
					} else if (!this.fallback(d, text)) {
						this.stop();
						frappe.show_alert({ message: "This browser cannot play the voice.", indicator: "orange" });
					}
				},
				error: () => {
					if (!this.fallback({}, text)) this.stop();
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
			if (!r.ok || d.exc_type || d.exception) throw new Error(server_error(d, "Could not convert your voice to text."));
			return ((d.message || {}).text || "").trim();
		}));
	}

	function mic_help() {
		frappe.msgprint({
			title: "Microphone is blocked on this page",
			indicator: "orange",
			message:
				"Browsers allow the microphone only on <b>https</b> pages. Do one of these:<br><br>" +
				"<b>1.</b> In Chrome open <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>, " +
				"enter <code>" + frappe.utils.escape_html(window.location.origin) + "</code>, " +
				"set it to <b>Enabled</b> and click <b>Relaunch</b>.<br>" +
				"<b>2.</b> Or run the ERP on https (SSL).<br><br>" +
				"Then tap the mic button and click <b>Allow</b>.",
		});
	}

	// ---------------------------------------------------------------- tables: chart + Excel
	const EXPORT_URL = "/api/method/ats_assistant.assistant.api.export_table";

	function num(v) {
		const t = String(v == null ? "" : v).replace(/[,\s%]|rs\.?|pkr/gi, "");
		return t !== "" && !isNaN(t) ? parseFloat(t) : null;
	}

	function download_excel(heads, rows, title) {
		const fd = new FormData();
		fd.append("data", JSON.stringify([heads].concat(rows)));
		fd.append("title", title || "ATS Assistant");
		fetch(EXPORT_URL, {
			method: "POST",
			body: fd,
			credentials: "same-origin",
			headers: { "X-Frappe-CSRF-Token": frappe.csrf_token || "" },
		})
			.then((r) => { if (!r.ok) throw new Error(r.status); return r.blob(); })
			.then((blob) => {
				const a = document.createElement("a");
				a.href = URL.createObjectURL(blob);
				a.download = String(title || "ats_assistant").replace(/[^a-z0-9]+/gi, "_").toLowerCase() + "_" + frappe.datetime.now_date() + ".xlsx";
				document.body.appendChild(a);
				a.click();
				setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
			})
			.catch(() => frappe.show_alert({ message: "Excel download failed. Please try again.", indicator: "red" }));
	}

	const COLORS = ["#d23a2e", "#2f6fdf", "#1f9d55", "#f59e0b", "#8b5cf6", "#0ea5e9", "#ec4899", "#64748b", "#14b8a6", "#a16207"];
	const TOTAL_ROW = /^\s*(total|grand total|kul|sum|overall|total active employees)\b/i;

	function chart_rows(rows) {
		// leave out "Total" rows, and "IT - ATS" -> "IT" when all labels share the company suffix
		rows = rows.filter((r) => !TOTAL_ROW.test(String(r[0])));
		const suffix = rows.length ? (String(rows[0][0]).match(/ - [^-]+$/) || [""])[0] : "";
		if (suffix && rows.every((r) => String(r[0]).endsWith(suffix))) {
			rows = rows.map((r) => [String(r[0]).slice(0, -suffix.length)].concat(r.slice(1)));
		}
		return rows;
	}

	function numeric_columns(heads, rows) {
		return heads.map((h, i) => i).filter((i) =>
			i > 0 && rows.filter((r) => num(r[i]) !== null).length >= Math.max(1, Math.ceil(rows.length * 0.8)));
	}

	function bar_chart(heads, rows, col) {
		const top = rows.slice().sort((a, b) => (num(b[col]) || 0) - (num(a[col]) || 0)).slice(0, 25);
		const max = Math.max.apply(null, top.map((r) => Math.max(0, num(r[col]) || 0)).concat([1]));
		let html = "";
		top.forEach((r, i) => {
			const v = Math.max(0, num(r[col]) || 0);
			html += '<div class="ats-ai-bar-row"><span class="lbl" title="' + esc(r[0]) + '">' + esc(r[0]) + "</span>" +
				'<span class="bar"><i style="width:' + Math.max(1, Math.round((v / max) * 100)) + "%;background:" +
				COLORS[i % COLORS.length] + '"></i></span><span class="val">' + esc(r[col]) + "</span></div>";
		});
		if (rows.length > top.length) html += '<div class="ats-ai-chart-note">Top ' + top.length + " of " + rows.length + "</div>";
		return html;
	}

	function pie_chart(heads, rows, col) {
		const vals = rows.map((r) => Math.max(0, num(r[col]) || 0));
		const total = vals.reduce((a, b) => a + b, 0) || 1;
		const R = 42, C = 2 * Math.PI * R;
		let offset = 0, arcs = "", legend = "";
		vals.forEach((v, i) => {
			const len = (v / total) * C;
			const color = COLORS[i % COLORS.length];
			arcs += '<circle r="' + R + '" cx="60" cy="60" fill="none" stroke="' + color + '" stroke-width="22" ' +
				'stroke-dasharray="' + len.toFixed(2) + " " + (C - len).toFixed(2) + '" stroke-dashoffset="' + (-offset).toFixed(2) + '"></circle>';
			offset += len;
			legend += '<div class="lg"><i style="background:' + color + '"></i><span class="lbl">' + esc(rows[i][0]) +
				'</span><b>' + esc(rows[i][col]) + "</b><em>" + Math.round((v / total) * 100) + "%</em></div>";
		});
		return '<div class="ats-ai-pie"><svg viewBox="0 0 120 120" width="120" height="120"><g transform="rotate(-90 60 60)">' +
			arcs + '</g><text x="60" y="64" text-anchor="middle" class="ttl">' + esc(String(Math.round(total * 100) / 100)) +
			'</text></svg><div class="legend">' + legend + "</div></div>";
	}

	// Chart box with Bar / Pie switch and a column picker
	function chart_box(heads, rows, auto) {
		rows = chart_rows(rows);
		const cols = numeric_columns(heads, rows);
		if (!cols.length || rows.length < 2) return null;
		const box = document.createElement("div");
		box.className = "ats-ai-chart";
		let col = cols[0];
		let type = rows.length <= 6 && cols.length === 1 ? "pie" : "bar";
		const draw = () => {
			let head = '<div class="ats-ai-chart-head"><span class="ats-ai-chart-title">' + esc(heads[col]) + "</span>";
			head += '<span class="sw"><button data-t="bar" class="' + (type === "bar" ? "on" : "") + '">Bar</button>' +
				'<button data-t="pie" class="' + (type === "pie" ? "on" : "") + '">Pie</button></span>';
			if (cols.length > 1) {
				head += '<select>' + cols.map((i) => '<option value="' + i + '"' + (i === col ? " selected" : "") + ">" +
					esc(heads[i]) + "</option>").join("") + "</select>";
			}
			box.innerHTML = head + "</div>" + (type === "pie" ? pie_chart(heads, rows, col) : bar_chart(heads, rows, col));
			box.querySelectorAll(".sw button").forEach((b) => b.addEventListener("click", () => { type = b.dataset.t; draw(); }));
			const sel = box.querySelector("select");
			if (sel) sel.addEventListener("change", () => { col = parseInt(sel.value, 10); draw(); });
		};
		draw();
		box.style.display = auto ? "block" : "none";
		return box;
	}

	function table_rows(table) {
		const heads = Array.from(table.querySelectorAll("thead th")).map((th) => th.textContent.trim());
		const rows = Array.from(table.querySelectorAll("tbody tr")).map((tr) =>
			Array.from(tr.children).map((td) => td.textContent.trim()));
		return { heads: heads, rows: rows };
	}

	function enhance_tables(container, auto_chart) {
		container.querySelectorAll(".ats-ai-tw").forEach((wrap) => {
			const table = wrap.querySelector("table");
			if (!table || wrap.dataset.tools) return;
			wrap.dataset.tools = "1";
			const t = table_rows(table);
			if (!t.rows.length) return;
			const bar = document.createElement("div");
			bar.className = "ats-ai-tools";
			const chart = chart_box(t.heads, t.rows, auto_chart);
			if (chart) {
				const btn = document.createElement("button");
				btn.textContent = "📊 Chart";
				btn.addEventListener("click", () => { chart.style.display = chart.style.display === "none" ? "block" : "none"; });
				bar.appendChild(btn);
			}
			const xl = document.createElement("button");
			xl.textContent = "⬇ Excel";
			xl.addEventListener("click", () => download_excel(t.heads, t.rows));
			bar.appendChild(xl);
			wrap.after(bar);
			if (chart) bar.after(chart);
		});
	}

	// Full data behind the answer: "View all" opens a big window with search, sort, chart and Excel
	function show_dataset(ds) {
		const d = new frappe.ui.Dialog({ title: ds.title + " (" + ds.total + ")", size: "extra-large" });
		const root = d.$body[0];
		root.innerHTML =
			'<div class="ats-ai-ds"><div class="ats-ai-ds-bar"><input type="search" placeholder="Search…">' +
			'<span class="cnt"></span><button class="btn btn-xs btn-default ch">📊 Chart</button>' +
			'<button class="btn btn-xs btn-default xl">⬇ Excel</button></div><div class="ats-ai-ds-chart"></div>' +
			'<div class="ats-ai-ds-table"></div></div>';
		let rows = ds.rows.slice();
		let sort = { col: -1, asc: true };
		const box = root.querySelector(".ats-ai-ds-table");
		const cnt = root.querySelector(".cnt");
		const search = root.querySelector("input");
		const visible = () => {
			const q = search.value.trim().toLowerCase();
			return q ? rows.filter((r) => r.some((c) => String(c == null ? "" : c).toLowerCase().includes(q))) : rows;
		};
		const paint = () => {
			const list = visible();
			cnt.textContent = list.length + " of " + ds.total + (ds.total > ds.rows.length ? " (first " + ds.rows.length + " loaded)" : "");
			let html = "<table><thead><tr>" + ds.columns.map((c, i) => '<th data-i="' + i + '">' + esc(c) +
				(sort.col === i ? (sort.asc ? " ▲" : " ▼") : "") + "</th>").join("") + "</tr></thead><tbody>";
			list.slice(0, 1000).forEach((r) => { html += "<tr>" + r.map((c) => "<td>" + esc(c == null ? "" : c) + "</td>").join("") + "</tr>"; });
			box.innerHTML = html + "</tbody></table>" + (list.length > 1000 ? '<div class="ats-ai-chart-note">Showing 1000 rows - use Excel for all.</div>' : "");
			box.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
				const i = parseInt(th.dataset.i, 10);
				sort = { col: i, asc: sort.col === i ? !sort.asc : true };
				rows.sort((a, b) => {
					const x = num(a[i]), y = num(b[i]);
					const c = x !== null && y !== null ? x - y : String(a[i] == null ? "" : a[i]).localeCompare(String(b[i] == null ? "" : b[i]));
					return sort.asc ? c : -c;
				});
				paint();
			}));
		};
		search.addEventListener("input", paint);
		root.querySelector(".xl").addEventListener("click", () => download_excel(ds.columns, visible(), ds.title));
		const chart_area = root.querySelector(".ats-ai-ds-chart");
		const ch = root.querySelector(".ch");
		const test = chart_box(ds.columns, ds.rows.slice(0, 50), true);
		if (!test) ch.style.display = "none";
		ch.addEventListener("click", () => {
			if (chart_area.firstChild) { chart_area.innerHTML = ""; return; }
			const c = chart_box(ds.columns, visible().slice(0, 50), true);
			if (c) chart_area.appendChild(c);
		});
		paint();
		d.show();
	}

	function dataset_strip(datasets) {
		const wrap = document.createElement("div");
		wrap.className = "ats-ai-datasets";
		(datasets || []).forEach((ds) => {
			if (!ds || !ds.rows || !ds.rows.length) return;
			const row = document.createElement("div");
			row.className = "ats-ai-ds-row";
			row.innerHTML = '<span title="' + esc(ds.title) + '">📋 ' + esc(ds.title) + " · " + ds.total + " rows</span>";
			const view = document.createElement("button");
			view.textContent = "View all";
			view.addEventListener("click", () => show_dataset(ds));
			const xl = document.createElement("button");
			xl.textContent = "⬇ Excel";
			xl.addEventListener("click", () => download_excel(ds.columns, ds.rows, ds.title));
			row.appendChild(view);
			row.appendChild(xl);
			wrap.appendChild(row);
		});
		return wrap.children.length ? wrap : null;
	}

	// ---------------------------------------------------------------- page context
	// What the user has open (a record, a list or a report), so "iski attendance" needs no ID.
	const muted = {};
	function route_key() {
		try { return (frappe.get_route() || []).join("/"); } catch (e) { return ""; }
	}
	function page_context() {
		let route = [];
		try { route = frappe.get_route() || []; } catch (e) { return null; }
		if (muted[route.join("/")]) return null;
		try {
			if (route[0] === "Form" && window.cur_frm && cur_frm.doctype === route[1] && cur_frm.doc
				&& !cur_frm.is_new() && cur_frm.doctype !== "ATS AI Log") {
				const d = cur_frm.doc;
				const tf = cur_frm.meta && cur_frm.meta.title_field;
				const title = (tf && d[tf]) || d.employee_name || d.title || d.item_name || d.customer_name
					|| d.supplier_name || d.machine_name || "";
				return { type: "form", doctype: cur_frm.doctype, name: d.name, title: String(title || "").slice(0, 140),
					label: "📄 " + __(cur_frm.doctype) + ": " + (title && title !== d.name ? title + " (" + d.name + ")" : d.name) };
			}
			if (route[0] === "List" && window.cur_list && cur_list.doctype === route[1]) {
				let filters = [];
				try { filters = cur_list.get_filters_for_args ? cur_list.get_filters_for_args() : cur_list.filters || []; } catch (e) { filters = []; }
				filters = (filters || []).filter((f) => Array.isArray(f)).slice(0, 10);
				return { type: "list", doctype: cur_list.doctype, filters: filters,
					label: "📋 " + __(cur_list.doctype) + " list" + (filters.length ? " (" + filters.length + " filter" + (filters.length > 1 ? "s" : "") + ")" : "") };
			}
			if (route[0] === "query-report" && frappe.query_report && frappe.query_report.report_name === route[1]) {
				let filters = {};
				try { filters = frappe.query_report.get_filter_values(false) || {}; } catch (e) { filters = {}; }
				return { type: "report", report: route[1], filters: filters, label: "📊 " + __(route[1]) };
			}
		} catch (e) {
			return null;
		}
		return null;
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
			'<div><button class="ats-ai-auto" title="My automations">⏰</button><button class="ats-ai-max" title="Bigger window">⤢</button><button class="ats-ai-voice" title="Read answers aloud (on/off)"></button>' +
			'<button class="ats-ai-clear" title="Clear chat">Clear</button><button class="ats-ai-close" title="Close">&times;</button></div></div>' +
			'<div class="ats-ai-body"></div>' +
			'<div class="ats-ai-ctx" title="The assistant knows this page: ask e.g. &quot;iski attendance&quot;"><span></span>' +
			'<button title="Do not use this page">&times;</button></div>' +
			'<div class="ats-ai-foot">' +
			'<button class="ats-ai-mic" title="Ask by voice">' + ICON.mic + "</button>" +
			'<textarea rows="1" placeholder="Ask anything or tap the mic…"></textarea>' +
			'<button class="ats-ai-send" title="Send">' + ICON.send + "</button></div>";

		document.body.appendChild(fab);
		document.body.appendChild(panel);

		const body = panel.querySelector(".ats-ai-body");
		const input = panel.querySelector("textarea");
		const send_btn = panel.querySelector(".ats-ai-send");
		const mic_btn = panel.querySelector(".ats-ai-mic");
		const voice_btn = panel.querySelector(".ats-ai-voice");
		const placeholder = input.placeholder;
		const ctx_bar = panel.querySelector(".ats-ai-ctx");
		function paint_ctx() {
			const c = page_context();
			ctx_bar.classList.toggle("show", !!c);
			ctx_bar.querySelector("span").textContent = c ? c.label : "";
			return c;
		}
		ctx_bar.querySelector("button").addEventListener("click", () => {
			muted[route_key()] = 1;
			paint_ctx();
		});
		const repaint_ctx = () => {
			if (!panel.classList.contains("open")) return;
			[300, 1200].forEach((t) => setTimeout(paint_ctx, t));
		};
		if (frappe.router && frappe.router.on) frappe.router.on("change", repaint_ctx);
		$(document).on("form-refresh", repaint_ctx);
		let chat = load();

		// ---------------- automation results (scheduled questions / reminders)
		let inbox = [];
		const seen = new Set(chat.filter((c) => c.auto_id).map((c) => c.auto_id));
		const badge = document.createElement("span");
		badge.className = "ats-ai-badge";
		fab.appendChild(badge);
		function paint_badge() {
			badge.textContent = inbox.length > 9 ? "9+" : String(inbox.length);
			badge.style.display = inbox.length ? "flex" : "none";
		}
		function flush_inbox() {
			if (!inbox.length || !panel.classList.contains("open")) return;
			const items = inbox;
			inbox = [];
			paint_badge();
			const welcome = body.querySelector(".ats-ai-welcome");
			if (welcome) welcome.remove();
			items.forEach((d) => {
				const head = (d.kind === "Reminder" ? "⏰ **Reminder**" : "📊 **" + (d.title || "Automation") + "**") +
					(d.time ? " · " + d.time.slice(11, 16) : "");
				const content = head + "\n\n" + (d.answer || "");
				chat.push({ role: "assistant", content: content, auto: 1, auto_id: d.id });
				bubble("assistant", content, "automation");
			});
			save(chat);
			body.scrollTop = body.scrollHeight;
			frappe.call({ method: "ats_assistant.assistant.automation.mark_read",
				args: { ids: JSON.stringify(items.map((d) => d.id)) }, freeze: false });
		}
		function add_to_inbox(list) {
			const known = new Set(inbox.map((d) => d.id));
			(list || []).forEach((d) => {
				if (d && d.id && !known.has(d.id) && !seen.has(d.id)) {
					seen.add(d.id);
					inbox.push(d);
				}
			});
			paint_badge();
			flush_inbox();
		}
		function load_inbox() {
			frappe.call({ method: "ats_assistant.assistant.automation.get_inbox", freeze: false,
				callback: (r) => add_to_inbox(r.message || []), error: () => {} });
		}
		if (frappe.realtime && frappe.realtime.on) {
			frappe.realtime.on("ats_ai_delivery", (d) => {
				add_to_inbox([d]);
				if (!panel.classList.contains("open")) {
					const label = (d.kind === "Reminder" ? "⏰ Reminder" : "📊 " + esc(d.title || "Automation")) +
						" is ready in the assistant";
					const a = frappe.show_alert({ message: label, indicator: d.error ? "red" : "blue" }, 10);
					if (a && a.on) a.css("cursor", "pointer").on("click", () => fab.click());
				}
			});
		}
		paint_badge();
		load_inbox();
		let busy = false;
		let current = {};
		// live status from the server ("Checking attendance…", "Running the report…")
		if (frappe.realtime && frappe.realtime.on) {
			frappe.realtime.on("ats_ai_progress", (d) => {
				if (d && d.rid && d.rid === current.rid) current.text = d.text;
			});
		}
		let auto_voice = pref("voice") === "1";

		function paint_voice() {
			voice_btn.innerHTML = auto_voice ? ICON.on : ICON.off;
			voice_btn.classList.toggle("on", auto_voice);
			voice_btn.title = auto_voice ? "Voice ON: every answer is read aloud" : "Voice OFF";
		}
		paint_voice();

		function bubble(role, text, meta, opts) {
			opts = opts || {};
			const div = document.createElement("div");
			div.className = "ats-ai-msg " + (role === "user" ? "me" : "bot");
			const content = document.createElement("div");
			content.className = "ats-ai-content";
			div.appendChild(content);
			const finish = () => {
				content.innerHTML = role === "user" ? "<p>" + esc(text) + "</p>" : md(text);
				if (role !== "user") {
					enhance_tables(content, opts.chart);
					const strip = dataset_strip(opts.datasets);
					if (strip) content.appendChild(strip);
				}
				body.scrollTop = body.scrollHeight;
				if (opts.done) opts.done();
			};
			if (opts.typing && role !== "user" && text) {
				// answer appears word by word
				const steps = Math.min(45, Math.max(6, Math.ceil(text.length / 14)));
				let i = 0;
				const tick = () => {
					i++;
					content.innerHTML = md(text.slice(0, Math.ceil((text.length * i) / steps)));
					body.scrollTop = body.scrollHeight;
					if (i < steps) setTimeout(tick, 20);
					else finish();
				};
				tick();
			} else {
				finish();
			}
			if (role !== "user" || meta) {
				const m = document.createElement("div");
				m.className = "ats-ai-meta";
				if (meta) m.appendChild(document.createTextNode(meta));
				if (role !== "user" && text) {
					const play = document.createElement("button");
					play.className = "ats-ai-play";
					play.innerHTML = ICON.play + " Listen";
					play.addEventListener("click", () => {
						if (play.classList.contains("playing")) Speaker.stop();
						else Speaker.play(text, play);
					});
					m.appendChild(play);
					const copy = document.createElement("button");
					copy.className = "ats-ai-play";
					copy.textContent = "Copy";
					copy.addEventListener("click", () => {
						const done = () => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1500); };
						if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
						else {
							const ta = document.createElement("textarea");
							ta.value = text; document.body.appendChild(ta); ta.select();
							try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
							ta.remove();
						}
					});
					m.appendChild(copy);
				}
				div.appendChild(m);
			}
			body.appendChild(div);
			body.scrollTop = body.scrollHeight;
			return div;
		}

		const SUGGESTIONS = [
			"Today's attendance", "Who is late today?", "Department-wise attendance chart",
			"How many active employees?", "Who is absent today?", "Open work orders",
		];

		function render() {
			body.innerHTML = "";
			if (!chat.length) {
				const w = document.createElement("div");
				w.className = "ats-ai-welcome";
				w.innerHTML = "<b>Hi " + esc((frappe.session.user_fullname || "").split(" ")[0] || "there") + "!</b>";
				body.appendChild(w);
			}
			chat.forEach((c) => bubble(c.role, c.content, "", { datasets: c.datasets }));
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
			const history = chat.slice(-8).map((c) => ({ role: c.role, content: c.content, page: c.page || "" }));
			const welcome = body.querySelector(".ats-ai-welcome");
			if (welcome) welcome.remove();
			const ctx = paint_ctx();
			chat.push({ role: "user", content: q, page: ctx ? ctx.label.replace(/^\S+\s/, "") : "" });
			save(chat);
			bubble("user", q, (via_voice ? "🎤 " : "") + (ctx ? ctx.label : ""));
			const wait = document.createElement("div");
			wait.className = "ats-ai-msg bot ats-ai-wait";
			wait.innerHTML = "<p>…</p>";
			body.appendChild(wait);
			body.scrollTop = body.scrollHeight;
			const started = Date.now();
			const rid = Math.random().toString(36).slice(2, 12);
			current = { rid: rid, text: "Thinking" };
			const paint = () => {
				wait.innerHTML = "<p>" + esc(current.text) + "… " + Math.round((Date.now() - started) / 1000) + "s</p>";
			};
			const timer = setInterval(paint, 1000);

			frappe.call({
				method: API,
				args: {
					question: q, history: JSON.stringify(history), voice: via_voice ? 1 : 0, rid: rid,
					context: ctx ? JSON.stringify({ type: ctx.type, doctype: ctx.doctype, name: ctx.name, title: ctx.title,
						filters: ctx.filters, report: ctx.report }) : null,
				},
				freeze: false,
				callback: (r) => {
					const d = r.message || {};
					const ans = d.answer || "No answer received.";
					const datasets = d.datasets || [];
					chat.push({ role: "assistant", content: ans, datasets: datasets.filter((x) => x.rows && x.rows.length <= 300) });
					save(chat);
					wait.remove();
					const secs = Math.round((Date.now() - started) / 1000);
					const msg = bubble("assistant", ans, secs + "s", {
						typing: true,
						chart: /chart|graph|wise|compar|bar\b|pie/i.test(q),
						datasets: datasets,
					});
					if ((auto_voice || via_voice) && !d.error) {
						Speaker.play(ans, msg.querySelector(".ats-ai-play"));
					}
				},
				error: () => {
					wait.remove();
					bubble("assistant", "No response from the server. Please try again.");
				},
				always: () => {
					clearInterval(timer);
					current = {};
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
			input.placeholder = "Speak now… (tap the button again when done)";
			Recorder.start(
				(secs) => { input.placeholder = "Listening… " + secs + "s (tap to stop)"; },
				(blob, type) => {
					stop_recording_ui();
					if (blob.size < 800) return;
					input.placeholder = "Converting your voice to text…";
					mic_btn.disabled = true;
					upload_audio(blob, type)
						.then((text) => {
							input.placeholder = placeholder;
							mic_btn.disabled = false;
							if (!text) {
								frappe.show_alert({ message: "Could not understand the audio, please try again.", indicator: "orange" });
								return;
							}
							ask(text, true);
						})
						.catch((e) => {
							input.placeholder = placeholder;
							mic_btn.disabled = false;
							frappe.show_alert({ message: e.message || "Could not convert your voice to text.", indicator: "red" }, 7);
						});
				},
				(err) => {
					stop_recording_ui();
					if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
						frappe.show_alert({ message: "Microphone permission denied. Please allow the mic in your browser.", indicator: "red" }, 7);
					} else if (err && err.name === "NotFoundError") {
						frappe.show_alert({ message: "No microphone found on this computer.", indicator: "red" }, 7);
					} else {
						mic_help();
					}
				}
			);
		});

		panel.querySelector(".ats-ai-max").addEventListener("click", () => panel.classList.toggle("big"));
		panel.querySelector(".ats-ai-auto").addEventListener("click", () => {
			if (frappe.set_route) frappe.set_route("List", "ATS AI Automation");
			else window.location.href = "/app/ats-ai-automation";
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
				paint_ctx();
				flush_inbox();
				load_inbox();
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
			seen.clear();
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
