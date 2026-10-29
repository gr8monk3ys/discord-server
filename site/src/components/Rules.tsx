import rules from "@/data/generated/rules.json";
import games from "@/data/generated/games.json";

// Both lists are generated from server/layout.py by bot/logic/sitegen.py, so the page and
// the Discord server always agree (a drift test fails when they don't).

export type Segment = string | { code: string };
export type Rule = { lead: Segment[]; body: Segment[] };

function Segments({ parts }: { parts: Segment[] }) {
  return (
    <>
      {parts.map((part, i) => (typeof part === "string" ? part : <code key={i}>{part.code}</code>))}
    </>
  );
}

/** The rules as text segments: React escapes every one, so nothing in them is ever HTML. */
export function RuleList({ items = rules as Rule[] }: { items?: Rule[] }) {
  return (
    <ol className="rules">
      {items.map((rule, i) => (
        <li key={i}>
          <span>
            <strong>
              <Segments parts={rule.lead} />
            </strong>
            {rule.body.length ? " " : null}
            <Segments parts={rule.body} />
          </span>
        </li>
      ))}
    </ol>
  );
}

export function GameList() {
  return (
    <ul className="games">
      {(games as string[]).map((name) => (
        <li key={name} className="game">
          <span>{name}</span>
        </li>
      ))}
    </ul>
  );
}
