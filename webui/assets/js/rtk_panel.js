// RTK sidebar panel component (auto-loaded by the webui extension system
// from <module>/webui/assets/js/). Talks to GET /api/ext/rtk/stats, whose
// response is wrapped by the webui API layer in {data, success}.
function rtkPanel() {
    return {
        data: null,
        error: "",
        loading: false,
        lastLoad: null,

        async load() {
            if (this.loading) return;
            this.loading = true;
            this.error = "";
            try {
                const res = await fetch("/api/ext/rtk/stats", { headers: { "Accept": "application/json" } });
                if (!res.ok) throw new Error("HTTP " + res.status);
                const json = await res.json();
                if (json.success === false) throw new Error(String(json.data || "unknown error"));
                this.data = json.data ?? json;
                this.lastLoad = new Date().toLocaleTimeString();
            } catch (e) {
                this.error = e && e.message ? e.message : String(e);
            }
            this.loading = false;
        },

        meterWidth() {
            const value = this.data && this.data.gain ? this.data.gain.efficiency : null;
            if (value == null) return 0;
            return Math.min(100, Math.max(0, Number(value) || 0));
        }
    };
}
