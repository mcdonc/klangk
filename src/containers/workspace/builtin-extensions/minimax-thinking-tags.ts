/**
 * Minimax Thinking Tags Extension
 *
 * Fixes thinking content from minimax models that include literal
 * <think>I'm thinking...</think> tags instead of proper thinking blocks.
 *
 * This extension processes assistant messages in the message_end event
 * and converts text content containing thinking tags to proper thinking blocks.
 *
 * Usage:
 *   pi --extension ~/.pi/agent/extensions/minimax-thinking-tags
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const THINKING_START = "<think>";
const THINKING_END = "</think>";
const THINKING_TAG_PATTERN = new RegExp(
  THINKING_START + "([\\s\\S]*?)" + THINKING_END,
  "g",
);

type ContentItem =
  | { type: "text"; text: string }
  | { type: "thinking"; thinking: string; thinkingSignature: string };

export default function (pi: ExtensionAPI) {
  pi.on("message_end", (event, _ctx) => {
    if (event.message.role !== "assistant") {
      return;
    }

    // Check if any text content contains thinking tags (reset lastIndex first)
    THINKING_TAG_PATTERN.lastIndex = 0;
    let hasThinkingTags = false;
    for (const content of event.message.content) {
      if (content.type === "text" && content.text) {
        if (THINKING_TAG_PATTERN.test(content.text)) {
          hasThinkingTags = true;
          break;
        }
      }
    }
    if (!hasThinkingTags) {
      return;
    }

    // Transform content
    const newContent: ContentItem[] = [];

    for (const content of event.message.content) {
      if (content.type === "text" && content.text) {
        const text = content.text;
        THINKING_TAG_PATTERN.lastIndex = 0;

        let lastIndex = 0;
        let match: RegExpExecArray | null;

        while ((match = THINKING_TAG_PATTERN.exec(text)) !== null) {
          // Text before this tag
          const beforeText = text.slice(lastIndex, match.index);
          if (beforeText.trim()) {
            newContent.push({ type: "text", text: beforeText });
          }

          // Add thinking block
          newContent.push({
            type: "thinking",
            thinking: match[1],
            thinkingSignature: "",
          });

          lastIndex = match.index + match[0].length;
        }

        // Remaining text after last tag
        const remainingText = text.slice(lastIndex);
        if (remainingText.trim()) {
          newContent.push({ type: "text", text: remainingText });
        }
      } else {
        newContent.push(content);
      }
    }

    return { message: { ...event.message, content: newContent } };
  });
}
