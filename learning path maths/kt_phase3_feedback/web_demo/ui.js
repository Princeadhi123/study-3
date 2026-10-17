"use strict";

/* Shared presentation helpers for the demo pages. Plain DOM and
   textContent only — no HTML strings, no inputs, no behaviour, and no
   inferences about session data. */

const DemoUI = Object.freeze((() => {
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== null && text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }

  function heading(title, description = "", {eyebrow = "", level = 3} = {}) {
    const header = node("header", null, "ui-heading");
    if (eyebrow) header.appendChild(node("p", eyebrow, "eyebrow"));
    const depth = Math.min(6, Math.max(1, level | 0));
    header.appendChild(node(`h${depth}`, title));
    if (description) header.appendChild(node("p", description, "meta"));
    return header;
  }

  function section(title, description = "", {className = "", level = 3} = {}) {
    const section = node("section", null,
      `ui-section${className ? ` ${className}` : ""}`);
    if (title) section.appendChild(heading(title, description, {level}));
    return section;
  }

  function disclosure(title, key = null, {className = ""} = {}) {
    const details = node("details", null,
      `ui-disclosure${className ? ` ${className}` : ""}`);
    if (key) details.dataset.disclosureKey = key;
    details.appendChild(node("summary", title));
    return details;
  }

  function stat(label, value, note = "") {
    const box = node("div", null, "ui-stat");
    box.appendChild(node("span", label, "ui-stat-label"));
    box.appendChild(node("strong",
      value === null || value === undefined ? "Unavailable" : String(value),
      "ui-stat-value"));
    if (note) box.appendChild(node("span", note, "ui-stat-note"));
    return box;
  }

  /* Structured task-card summary: number label plus the topic and
     subtopic path as separate spans; status badges are appended by the
     caller after this node. */
  function taskSummary(label, {topic = "", subtopic = ""} = {}) {
    const wrapper = node("span", null, "skill-task-label");
    wrapper.appendChild(node("span", label, "task-num"));
    const path = node("span", null, "task-path");
    if (topic) path.appendChild(node("span", topic, "task-topic"));
    if (subtopic) path.appendChild(node("span", subtopic, "task-subtopic"));
    wrapper.appendChild(path);
    return wrapper;
  }

  return {heading, section, disclosure, stat, taskSummary};
})());
