(function (root) {
  "use strict";
  class InputRouter {
    constructor(write) {
      this.write = write;
      this.enabled = false;
      this.selected = new Set();
      this.records = new Map();
    }
    register(id, generation = 1) {
      this.records.set(id, { generation, running: false });
      this.selected.delete(id);
    }
    update(id, generation, running) {
      const old = this.records.get(id);
      if (!old || old.generation !== generation || !running)
        this.selected.delete(id);
      this.records.set(id, { generation, running });
    }
    remove(id) {
      this.records.delete(id);
      this.selected.delete(id);
    }
    select(id, checked) {
      if (checked && this.records.get(id)?.running) this.selected.add(id);
      else this.selected.delete(id);
    }
    selectAll() {
      for (const [id, record] of this.records) this.select(id, record.running);
    }
    clear() {
      this.selected.clear();
    }
    targets(source, human) {
      return human && this.enabled && this.selected.has(source)
        ? [...this.selected].filter((id) => this.records.get(id)?.running)
        : [source];
    }
    input(source, data, human = false) {
      for (const id of this.targets(source, human))
        if (this.records.get(id)?.running) this.write(id, data);
    }
  }
  if (typeof module !== "undefined" && module.exports)
    module.exports = { InputRouter };
  else root.InputRouter = InputRouter;
})(globalThis);
