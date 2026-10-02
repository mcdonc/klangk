/**
 * `/header` — print the Pi startup header on demand.
 *
 * The workspace provisions `quietStartup: true` in settings.json: the
 * startup header scrolls the web terminal on every launch and repeats
 * on every fresh shell. This extension registers `/header`, which
 * re-prints the same information the startup header shows — loaded
 * context files, skills, prompt templates, and extensions — as a
 * persistent session entry that never enters the LLM context.
 */

import { readdirSync } from "node:fs";
import { Container, Text } from "@earendil-works/pi-tui";
import type {
  CustomEntry,
  EntryRenderOptions,
  ExtensionAPI,
  ExtensionCommandContext,
  Theme,
} from "@earendil-works/pi-coding-agent";

const ENTRY_TYPE = "klangk-startup-header";

interface HeaderData {
  context: string[];
  skills: string[];
  prompts: string[];
  extensions: string[];
}

function contextPaths(ctx: ExtensionCommandContext): string[] {
  return (ctx.getSystemPromptOptions()?.contextFiles ?? []).map((f) => f.path);
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
  for (const dir of pi.getSettings().extensions ?? []) {
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      continue; // configured directory no longer exists
    }
    for (const entry of entries) {
      if (/\.(ts|js|mjs)$/.test(entry)) {
        labels.add(entry.replace(/\.(ts|js|mjs)$/, ""));
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
  const data = entry.data ?? {
    context: [],
    skills: [],
    prompts: [],
    extensions: [],
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
