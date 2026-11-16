"use strict";

function sortFileEntries(entries, key, descending) {
  const name = new Intl.Collator("ru", { numeric: true, sensitivity: "base" });
  return [...entries].sort((left, right) => {
    if (left.isDirectory !== right.isDirectory) return Number(right.isDirectory) - Number(left.isDirectory);
    const a = key === "size" && left.isDirectory ? null : left[key];
    const b = key === "size" && right.isDirectory ? null : right[key];
    if (a == null && b != null) return 1;
    if (b == null && a != null) return -1;
    const order = a == null ? 0 : typeof a === "number" ? a - b : name.compare(a, b);
    return (descending ? -order : order) || name.compare(left.name, right.name);
  });
}

if (typeof module !== "undefined") module.exports = { sortFileEntries };
