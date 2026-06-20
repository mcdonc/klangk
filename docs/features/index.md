# Features

## UI/Theme

- Dark theme inspired by GitHub's dark default (dark canvas background, surface cards, muted borders)
- Centralized color palette in `KColors` (`src/frontend/lib/theme/colors.dart`) — named tokens for backgrounds, borders, text, and accents
- Green Klangk logo (robot icon + lowercase "klangk" text) with matching green FAB and primary action buttons
- Full-width Terminal+Files tabs with background-only active state (no indicator bar), rounded bottom corners
- Slidable Debug panel on bottom (collapsed by default)
- Browser tab title updates per page ("Klangk - Login", "Klangk - Workspaces", "Klangk - workspace-name")

## Panel Layout

- Two-part split: tabbed panel on top (Terminal, Files tabs) and slidable Debug panel on bottom
- Debug panel collapsed by default, expandable via draggable horizontal divider
- All panels stay alive across switches (IndexedStack for tabs, always-mounted Debug)
- Debug pane receives events from the start, even before first viewed
