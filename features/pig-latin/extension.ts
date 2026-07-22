import { Type } from "@sinclair/typebox";

const VOWELS = new Set("aeiouAEIOU".split(""));

function toPigLatin(word: string): string {
  if (!word || !word[0].match(/[a-zA-Z]/)) return word;
  if (VOWELS.has(word[0])) return word + "yay";
  for (let i = 0; i < word.length; i++) {
    if (VOWELS.has(word[i])) {
      return word.slice(i) + word.slice(0, i) + "ay";
    }
  }
  return word + "ay";
}

function convert(text: string): string {
  return text.replace(/[a-zA-Z]+/g, (match) => toPigLatin(match));
}

export default function (pi: any) {
  pi.registerTool({
    name: "pig_latin",
    description:
      "Convert text to Pig Latin. Use when the user asks for Pig Latin translation.",
    parameters: Type.Object({
      text: Type.String({ description: "The text to convert to Pig Latin" }),
    }),
    async execute(_toolCallId: string, params: { text: string }) {
      const result = convert(params.text);
      return {
        content: [{ type: "text", text: result }],
        details: {},
      };
    },
  });
}
