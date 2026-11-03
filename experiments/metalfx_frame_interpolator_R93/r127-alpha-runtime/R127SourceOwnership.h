#ifndef R127_SOURCE_OWNERSHIP_H
#define R127_SOURCE_OWNERSHIP_H

#include <assert.h>
#include <stdint.h>

// Bounded ownership ledger. An FG obligation remains nonterminal until its
// WSI dispatch, and its slot must not be reused while that obligation exists.
#define R127_SOURCE_OWNERSHIP_CAPACITY 256u

typedef enum {
    R127SourceOwnerUnknown = 0,
    R127SourceOwnerDirect = 1,
    R127SourceOwnerSuppressed = 2,
    R127SourceOwnerDispatched = 3,
    R127SourceOwnerTitleDispatched = 4,
} R127SourceOwner;

typedef struct {
    uint64_t sequence;
    uint64_t sourcePresentOrdinal;
    uint32_t disposition;
} R127SourceOwnershipEntry;

typedef struct {
    R127SourceOwnershipEntry entries[R127_SOURCE_OWNERSHIP_CAPACITY];
    uint64_t overwriteNonTerminal;
    uint64_t slotRefusals;
} R127SourceOwnershipTable;

static inline R127SourceOwner R127SourceOwnershipRead(
    const R127SourceOwnershipTable *table, uint64_t sequence) {
    if (!table || sequence == 0) return R127SourceOwnerUnknown;
    const R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    return entry->sequence == sequence
        ? (R127SourceOwner)entry->disposition : R127SourceOwnerUnknown;
}

// The observer serializes this table with a short mutex. First decision wins,
// so a later ON cannot relabel a source that already selected title-direct WSI.
static inline R127SourceOwner R127SourceOwnershipSetIfUnknown(
    R127SourceOwnershipTable *table, uint64_t sequence,
    R127SourceOwner disposition) {
    if (!table || sequence == 0 || disposition == R127SourceOwnerUnknown)
        return R127SourceOwnerUnknown;
    assert(table->overwriteNonTerminal == 0);
    R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (entry->sequence == sequence)
        return (R127SourceOwner)entry->disposition;
    if (entry->sequence > sequence) {
        ++table->slotRefusals;
        return R127SourceOwnerUnknown;
    }
    if (entry->disposition == R127SourceOwnerSuppressed ||
        entry->disposition == R127SourceOwnerDirect) {
        ++table->slotRefusals;
        return R127SourceOwnerUnknown;
    }
    assert(entry->disposition != R127SourceOwnerSuppressed &&
           entry->disposition != R127SourceOwnerDirect);
    entry->sequence = sequence;
    entry->sourcePresentOrdinal = 0;
    entry->disposition = (uint32_t)disposition;
    return disposition;
}

// The caller serializes this with ownership commit and the actual WSI call.
// Duplicate and stale dispatch attempts cannot mutate a terminal record.
static inline int R127SourceOwnershipMarkDispatched(
    R127SourceOwnershipTable *table, uint64_t sequence) {
    if (!table || !sequence) return 0;
    R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (entry->sequence != sequence ||
        entry->disposition != R127SourceOwnerSuppressed) return 0;
    entry->disposition = R127SourceOwnerDispatched;
    return 1;
}

// Only for a failed provisional pump commit, before the title hook returns.
static inline int R127SourceOwnershipUndoProvisional(
    R127SourceOwnershipTable *table, uint64_t sequence) {
    if (!table || !sequence) return 0;
    R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (entry->sequence != sequence ||
        entry->disposition != R127SourceOwnerSuppressed) return 0;
    entry->sequence = 0;
    entry->sourcePresentOrdinal = 0;
    entry->disposition = R127SourceOwnerUnknown;
    return 1;
}

static inline int R127SourceOwnershipMarkTitleDispatched(
    R127SourceOwnershipTable *table, uint64_t sequence) {
    if (!table || !sequence) return 0;
    R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (entry->sequence != sequence ||
        entry->disposition != R127SourceOwnerDirect) return 0;
    entry->disposition = R127SourceOwnerTitleDispatched;
    return 1;
}

// Acquisition identity remains the ledger key. Presentation order is an
// independent value assigned at the title or FG WSI boundary.
static inline int R127SourceOwnershipSetPresentOrdinal(
    R127SourceOwnershipTable *table, uint64_t sequence,
    uint64_t sourcePresentOrdinal) {
    if (!table || !sequence || !sourcePresentOrdinal) return 0;
    R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (entry->sequence != sequence ||
        (entry->sourcePresentOrdinal != 0 &&
         entry->sourcePresentOrdinal != sourcePresentOrdinal)) return 0;
    entry->sourcePresentOrdinal = sourcePresentOrdinal;
    return 1;
}

static inline uint64_t R127SourceOwnershipPresentOrdinal(
    const R127SourceOwnershipTable *table, uint64_t sequence) {
    if (!table || !sequence) return 0;
    const R127SourceOwnershipEntry *entry =
        &table->entries[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    return entry->sequence == sequence ? entry->sourcePresentOrdinal : 0;
}

#endif
