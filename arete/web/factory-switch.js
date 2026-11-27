import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";

const endpoint = "/api/v1/factory/state";

class FactorySwitch extends HTMLElement {
  connectedCallback() {
    if (this.button) return;
    this.button = document.createElement("button");
    this.button.type = "button";
    this.button.setAttribute("role", "switch");
    this.button.setAttribute("aria-label", "Factory power");
    this.button.addEventListener("click", () => this.change());
    this.status = document.createElement("span");
    this.status.setAttribute("role", "status");
    this.details = document.createElement("details");
    this.summary = document.createElement("summary");
    this.list = document.createElement("ul");
    this.details.append(this.summary, this.list);
    this.append(this.button, this.status, this.details);
    this.render();
    this.refresh();
  }

  disconnectedCallback() {
    clearTimeout(this.timer);
    this.revision = (this.revision || 0) + 1;
    this.replaceChildren();
    this.button = null;
  }

  render() {
    const state = this.state;
    this.button.disabled = !state || this.writing || Boolean(this.unavailable);
    this.button.textContent = state ? (state.enabled ? "Stop factory" : "Resume factory") : "Factory control";
    this.button.dataset.stop = String(Boolean(state?.enabled));
    this.button.setAttribute("aria-checked", String(Boolean(state?.enabled)));
    const label = { on: "Factory on", stopping: "Stopping", off: "Factory off", resuming: "Resuming" };
    this.status.textContent = this.unavailable || this.problem || (state ? label[state.phase] +
      (state.unconfirmed ? ` · ${state.unconfirmed} unconfirmed` : state.phase === "resuming" ? ` · ${state.paused} waiting` : "") : "Connecting…");
    const targets = state?.targets || [];
    this.details.hidden = !targets.length;
    this.summary.textContent = `Affected work (${targets.length})`;
    this.list.replaceChildren(...targets.map((target) => {
      const item = document.createElement("li");
      item.textContent = `${target.id} · ${target.reason || target.phase}`;
      return item;
    }));
  }

  async refresh() {
    const revision = this.revision = (this.revision || 0) + 1;
    clearTimeout(this.timer);
    try {
      const response = await webFetch(endpoint, { cache: "no-store" });
      if (!response.ok) throw new Error(`Factory status unavailable (${response.status})`);
      const { factory } = await response.json();
      if (revision !== this.revision) return;
      this.state = factory;
      this.unavailable = "";
    } catch (error) {
      if (revision !== this.revision) return;
      this.unavailable = error.message;
    }
    if (!this.isConnected || revision !== this.revision) return;
    this.render();
    this.timer = setTimeout(() => this.refresh(), this.state?.phase === "on" ? 4000 : 1000);
  }

  async change() {
    if (this.button.disabled) return;
    clearTimeout(this.timer);
    this.revision++;
    this.writing = true;
    this.problem = "";
    this.render();
    try {
      const answer = await sendWrite(prepareWrite(endpoint, { enabled: !this.state.enabled }, this.state));
      this.state = answer.factory;
    } catch (error) {
      this.problem = error.message;
    } finally {
      this.writing = false;
      if (this.isConnected) await this.refresh();
    }
  }
}

customElements.define("factory-switch", FactorySwitch);
