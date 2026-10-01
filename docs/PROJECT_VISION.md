# Jarvis Project Vision

## Purpose

Build a personal assistant called Jarvis that brings together useful conversation, personal knowledge, and everyday tools. It should feel like one assistant even as its models, integrations, and devices evolve.

## Vision

Jarvis will be available first on this machine. A small local model served by Ollama will handle routine conversation and keep the basic experience useful without depending on a cloud service. When a task needs deeper reasoning, Jarvis will be able to hand it to Codex using the user's existing Codex account. The assistant will use MCP servers as explicit, replaceable integrations for services such as Gmail and Calendar, and Obsidian will hold the user's durable knowledge in a readable, user-owned form.

Over time, the same assistant should be able to move beyond this machine into a portable desk buddy with a microphone, speakers, and a small screen. A Raspberry Pi 4 or ESP32 may provide the hardware, with the choice made after the software experience and device requirements are clearer.

## Guiding principles

- **Useful locally first.** The initial experience runs on this machine with Ollama; cloud reasoning adds capability rather than being the only way to use Jarvis.
- **One assistant, swappable parts.** Keep model providers, MCP integrations, knowledge storage, and device interfaces behind clear boundaries so each can change without rebuilding the whole system.
- **The user owns their knowledge.** Store durable notes in Obsidian as ordinary files the user can inspect, edit, back up, and use without Jarvis.
- **Explicit access to personal services.** Connect Gmail through the Google API and Calendar through MCP, with narrow permissions and clear visibility into what Jarvis can read or change.
- **Confirm consequential actions.** Jarvis may prepare messages, events, and other changes, but should ask before sending, deleting, or making a commitment on the user's behalf.
- **Be clear about capability and source.** Distinguish local answers from cloud-assisted reasoning, and ground personal answers in the notes or service data actually consulted.
- **Design for the next form factor.** Keep the assistant's core usable through a local interface first, then add voice and a compact screen as a later device experience.

## Initial scope

The first milestone is a usable Jarvis on this machine:

1. Provide a simple conversational interface.
2. Connect to an Ollama model for routine local requests.
3. Provide an intentional route to Codex for tasks needing advanced cloud reasoning, using the user's Codex account.
4. Read and write selected notes in an Obsidian vault.
5. Add Gmail API and Calendar MCP integrations in stages, beginning with safe read access and requiring confirmation for changes.
6. Keep configuration, credentials, and conversation data local by default, with cloud use visible when it happens.

## Later direction

Once the desktop experience is dependable, explore a portable desk buddy. It should support hands-free input through a microphone, spoken responses through speakers, and a small screen for status or short text. The hardware platform remains open between Raspberry Pi 4 and ESP32 until the needs for local inference, connectivity, audio, display, power, and enclosure are understood.

## What success looks like

- Jarvis can answer everyday requests locally and remains useful when cloud reasoning is unavailable.
- The user can deliberately escalate a harder request to Codex and understand that it is using a cloud service.
- Jarvis can find and maintain relevant Obsidian notes without trapping knowledge in a proprietary database.
- Gmail and Calendar access is understandable, limited, and safe for real personal data.
- The core assistant can gain new MCP integrations and a voice device without being tied to one model or hardware board.

## Open decisions

- Which Ollama model and local interface best fit this machine.
- How the Codex account will be reached from the Jarvis application and what handoff experience is practical.
- Which Gmail API and Calendar MCP permission scopes to use.
- How Obsidian notes should be organized and which actions Jarvis may perform without confirmation.
- Whether Raspberry Pi 4 or ESP32 is appropriate once voice, display, and inference requirements are measured.

These decisions should be resolved through small working prototypes rather than assumed up front.
