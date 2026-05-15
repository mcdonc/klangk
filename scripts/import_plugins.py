#!/usr/bin/env python3
"""Generate frontend/lib/tools/plugins_generated.dart from plugins/*/plugin.dart.

Copies plugin .dart files into frontend/lib/tools/plugins/<name>/ so they're
within the Flutter package and can be imported normally.
"""

import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.environ.get("BARK_PLUGINS_DIR") or os.path.join(os.path.expanduser("~"), ".bark", "plugins")
DEST_DIR = os.path.join(ROOT, "frontend", "lib", "tools", "plugins")
OUTPUT = os.path.join(ROOT, "frontend", "lib", "tools", "plugins_generated.dart")


def find_and_copy_plugins():
    """Scan plugins/*/plugin.dart, copy into frontend, return metadata."""
    plugins = []
    if not os.path.isdir(PLUGINS_DIR):
        return plugins

    # Clean destination
    if os.path.isdir(DEST_DIR):
        shutil.rmtree(DEST_DIR)
    os.makedirs(DEST_DIR)

    for name in sorted(os.listdir(PLUGINS_DIR)):
        plugin_dir = os.path.join(PLUGINS_DIR, name)
        plugin_dart = os.path.join(plugin_dir, "plugin.dart")
        if not os.path.isfile(plugin_dart):
            continue

        with open(plugin_dart) as f:
            source = f.read()

        # Find class names extending ToolPlugin
        matches = re.findall(r"class\s+(\w+)\s+extends\s+ToolPlugin", source)
        if not matches:
            continue

        # Copy all .dart files from the plugin directory
        dest = os.path.join(DEST_DIR, name)
        os.makedirs(dest, exist_ok=True)
        for fname in os.listdir(plugin_dir):
            if fname.endswith(".dart"):
                shutil.copy2(os.path.join(plugin_dir, fname), dest)

        for class_name in matches:
            plugins.append({
                "dir": name,
                "class_name": class_name,
                "import_path": f"plugins/{name}/plugin.dart",
            })

    return plugins


def generate(plugins):
    lines = [
        "// GENERATED — do not edit. Run `python scripts/import_plugins.py` to regenerate.",
        "import 'tool_plugin.dart';",
        "",
    ]
    for p in plugins:
        lines.append(f"import '{p['import_path']}';")

    lines.append("")
    lines.append("List<ToolPlugin> createAllPlugins() {")
    lines.append("  return [")
    for p in plugins:
        lines.append(f"    {p['class_name']}(),")
    lines.append("  ];")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main():
    plugins = find_and_copy_plugins()
    output = generate(plugins)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        f.write(output)

    names = [p["class_name"] for p in plugins]
    print(f"Generated {OUTPUT} with {len(plugins)} plugins: {', '.join(names)}")


if __name__ == "__main__":
    main()
