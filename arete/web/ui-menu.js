import { h, Component } from "./vendor-preact.js";

let active = null;

export function dismissMenu() {
  if (!active) return false;
  active.close();
  return true;
}

export class Menu extends Component {
  state = { open: false };
  close = (restore = true) => {
    if (active === this) active = null;
    this.setState({ open: false });
    if (restore) this.opener?.focus();
  };
  outside = (event) => {
    if (this.state.open && !this.base.contains(event.target)) this.close(false);
  };
  componentDidMount() {
    document.addEventListener("pointerdown", this.outside);
  }
  componentWillUnmount() {
    document.removeEventListener("pointerdown", this.outside);
    if (active === this) active = null;
  }
  componentDidUpdate() {
    if (!this.state.open) return;
    const pop = this.base.querySelector(".ui-menu-pop");
    pop.style.transform = "";
    const pane = this.base.closest("[data-pane]").getBoundingClientRect();
    const box = pop.getBoundingClientRect();
    const shift = Math.min(0, Math.min(pane.right, window.innerWidth) - box.right - 6);
    pop.style.transform = `translateX(${shift}px)`;
  }
  render({ label, title, children }) {
    return h("span", { class: "ui-menu" },
      h("button", { type: "button", title, "aria-label": title || label, "aria-haspopup": "menu",
        "aria-expanded": this.state.open, ref: (el) => { this.opener = el; },
        onClick: () => {
          if (this.state.open) return this.close();
          dismissMenu();
          active = this;
          this.setState({ open: true });
        } }, label),
      this.state.open && h("div", { class: "ui-menu-pop", role: "menu" }, children(this.close)));
  }
}
