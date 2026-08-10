# Example specifications

These specifications exercise distinct controller and verification behaviors:

- `counters/`: reset, priority, wrapping, and saturation semantics.
- `arbiters/`: hierarchy, registered handshakes, and round-robin fairness.
- `streaming/`: FIFO and ready/valid backpressure behavior.

Before the 0.3.0 repository cleanup these JSON files lived at repository root.
The content was moved with Git history intact. `counters/simple_counter_relative.json`
is the former root-level relative-floorplan example; the current canonical smoke
case remains `specs/simple_counter.json`. Those two files are intentionally not
duplicates and must not be substituted without reviewing their floorplan and
formal-property differences.
