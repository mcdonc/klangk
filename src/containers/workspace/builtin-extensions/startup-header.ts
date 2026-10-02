/**
 * `/header` — print the Pi startup header on demand.
 *
 * The workspace provisions `quietStartup: true` in settings.json: the
 * startup header scrolls the web terminal on every launch and repeats
 * on every fresh shell. This extension registers `/header`, which
 * re-prints the startup header's sections — loaded context files,
 * skills, prompt templates, and extensions — as a persistent session
 * entry that never enters the LLM context. The sections are rebuilt
 * from live session data (pi 0.99.2 exposes no API to re-run its own
 * renderer), so edge cases can diverge from the startup header: the
 * Skills section is empty when `enableSkillCommands` is off, and the
 * Extensions section lists files found on disk rather than ones that
 * actually loaded.
 */

import { readdirSync } from "node:fs";
import * as path from "node:path";
import { Container, Text } from "@earendil-works/pi-tui";
import {
  getAgentDir,
  type CustomEntry,
  type EntryRenderOptions,
  type ExtensionAPI,
  type ExtensionCommandContext,
  type Theme,
} from "@earendil-works/pi-coding-agent";

const ENTRY_TYPE = "klangk-startup-header";

interface HeaderData {
  context: string[];
  skills: string[];
  prompts: string[];
  extensions: string[];
}

function contextPaths(ctx: ExtensionCommandContext): string[] {
  // Display cwd-relative like the startup header's formatContextPath.
  const prefix = ctx.cwd.endsWith("/") ? ctx.cwd : `${ctx.cwd}/`;
  return (ctx.getSystemPromptOptions()?.contextFiles ?? []).map((f) =>
    f.path.startsWith(prefix) ? f.path.slice(prefix.length) : f.path,
  );
}

function commandNames(pi: ExtensionAPI, source: "skill" | "prompt"): string[] {
  const names = pi
    .getCommands()
    .filter((command) => command.source === source)
    // Skill commands are registered as `/skill:<name>`; the startup
    // header lists bare skill names.
    .map((command) => command.name.replace(/^skill:/, ""));
  return [...new Set(names)].sort((a, b) => a.localeCompare(b));
}

function extensionLabels(pi: ExtensionAPI): string[] {
  const labels = new Set<string>();
  // Settings-configured dirs plus the auto-discovered user and project
  // dirs pi also loads extensions from.
  const dirs = [
    ...(pi.getSettings().extensions ?? []),
    path.join(getAgentDir(), "extensions"),
    path.join(process.cwd(), ".pi", "extensions"),
  ];
  for (const dir of dirs) {
    let entries: string[];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      continue; // missing dir, a configured file, or unreadable
    }
    for (const entry of entries) {
      // pi loads plain `.ts`/`.js` files and subdirectory extensions
      // (their own index/package manifest) from these dirs.
      if (entry.isDirectory()) {
        labels.add(entry.name);
      } else if (/\.(ts|js)$/.test(entry.name)) {
        labels.add(entry.name.replace(/\.(ts|js)$/, ""));
      }
    }
  }
  return [...labels].sort((a, b) => a.localeCompare(b));
}

function collectHeaderData(
  pi: ExtensionAPI,
  ctx: ExtensionCommandContext,
): HeaderData {
  return {
    context: contextPaths(ctx),
    skills: commandNames(pi, "skill"),
    prompts: commandNames(pi, "prompt").map((name) => `/${name}`),
    extensions: extensionLabels(pi),
  };
}

// One section of the header: `[Name]` heading plus the compact list
// (comma-joined, like the collapsed startup section) or, when expanded,
// one entry per line. Returns null when the section would be empty —
// the startup header skips empty sections too.
function headerSection(
  theme: Theme,
  name: string,
  compact: string[],
  expanded: string[],
): Container | null {
  if (compact.length === 0) {
    return null;
  }
  const section = new Container();
  section.addChild(new Text(theme.fg("mdHeading", `[${name}]`), 0, 0));
  const lines = expanded.length > 0 ? expanded : [compact.join(", ")];
  for (const line of lines) {
    section.addChild(new Text(theme.fg("dim", `  ${line}`), 0, 0));
  }
  return section;
}

function renderHeader(
  entry: CustomEntry<HeaderData>,
  options: EntryRenderOptions,
  theme: Theme,
): Container | undefined {
  // Per-key defaults so an entry persisted by an older version (or a
  // foreign shape) degrades to empty sections instead of throwing.
  const data = {
    context: [],
    skills: [],
    prompts: [],
    extensions: [],
    ...(entry.data ?? {}),
  };
  const expandedPaths = options.expanded;
  const container = new Container();
  const sections: Array<[string, string[]]> = [
    ["Context", data.context],
    ["Skills", data.skills],
    ["Prompts", data.prompts],
    ["Extensions", data.extensions],
  ];
  let added = 0;
  for (const [name, items] of sections) {
    const section = headerSection(
      theme,
      name,
      items,
      expandedPaths ? items : [],
    );
    if (section) {
      container.addChild(section);
      added += 1;
    }
  }
  return added > 0 ? container : undefined;
}

export default function (pi: ExtensionAPI) {
  pi.registerEntryRenderer(ENTRY_TYPE, renderHeader);
  pi.registerCommand("header", {
    description:
      "Print the startup header: context files, skills, prompts, extensions",
    handler: async (_args: string, ctx: ExtensionCommandContext) => {
      pi.appendEntry(ENTRY_TYPE, collectHeaderData(pi, ctx));
    },
  });
}
