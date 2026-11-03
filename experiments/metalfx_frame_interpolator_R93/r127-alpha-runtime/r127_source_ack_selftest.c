#include "R127SourceAck.h"
#include "R127SourceOwnership.h"

#include <stdio.h>

static unsigned failures;
static unsigned cases;

static void check(const char *name, bool pass) {
    ++cases;
    printf("  %-48s %s\n", name, pass ? "PASS" : "FAIL");
    if (!pass) ++failures;
}

int main(void) {
    R127SourceAckTracker tracker = {0};
    R127SourceAckBeginAtBoundary(&tracker, 100);
    check("minimum includes the ON boundary", R127SourceSequenceAtOrAfter(100, 100));
    check("minimum rejects a delayed pre-ON capture", !R127SourceSequenceAtOrAfter(99, 100));
    check("boundary starts immediately before its SOURCE", tracker.frontier == 99);

    check("later SOURCE waits behind a missing boundary", !R127SourceAckObserve(&tracker, 101));
    check("later acknowledgement cannot cross the gap", tracker.frontier == 99);
    check("boundary acknowledgement closes the contiguous range", R127SourceAckObserve(&tracker, 100));
    check("frontier advances over both physically displayed sources", tracker.frontier == 101);
    check("duplicate acknowledgement does not move frontier", !R127SourceAckObserve(&tracker, 100));
    check("next source advances the frontier", R127SourceAckObserve(&tracker, 102) &&
          tracker.frontier == 102);

    R127SourceAckBeginAtBoundary(&tracker, 500);
    check("new ON boundary resets the prior epoch", tracker.frontier == 499 &&
          tracker.pendingMask == 0);
    check("out-of-window acknowledgement fails closed", !R127SourceAckObserve(
          &tracker, 500 + R127_SOURCE_ACK_WINDOW + 1) && tracker.frontier == 499 &&
          tracker.outOfWindow == 1);
    check("boundary can still advance after an out-of-window event",
          R127SourceAckObserve(&tracker, 500) && tracker.frontier == 500);

    R127SourceOwnershipTable ownership = {0};
    check("direct present decision is recorded",
          R127SourceOwnershipSetIfUnknown(&ownership, 701,
              R127SourceOwnerDirect) == R127SourceOwnerDirect);
    check("ON cannot relabel an already-direct SOURCE",
          R127SourceOwnershipSetIfUnknown(&ownership, 701,
              R127SourceOwnerSuppressed) == R127SourceOwnerDirect);
    R127SourceAckBeginAtBoundary(&tracker, 700);
    check("sequence minimum alone would admit the direct SOURCE",
          R127SourceSequenceAtOrAfter(701, 700));
    check("direct SOURCE callback remains direct after ON",
          R127SourceOwnershipRead(&ownership, 701) == R127SourceOwnerDirect);
    check("runtime-suppressed decision is recorded",
          R127SourceOwnershipSetIfUnknown(&ownership, 702,
              R127SourceOwnerSuppressed) == R127SourceOwnerSuppressed);
    check("suppressed SOURCE is distinguishable for enqueue",
          R127SourceOwnershipRead(&ownership, 702) == R127SourceOwnerSuppressed);
    check("unrecorded sequence fails closed as unknown",
          R127SourceOwnershipRead(&ownership, 703) == R127SourceOwnerUnknown);
    check("undispatched title slot refuses collision",
          R127SourceOwnershipSetIfUnknown(&ownership,
              701 + R127_SOURCE_OWNERSHIP_CAPACITY,
              R127SourceOwnerDirect) == R127SourceOwnerUnknown &&
          R127SourceOwnershipMarkTitleDispatched(&ownership, 701));
    check("ring overwrite makes stale ownership unknown",
          R127SourceOwnershipSetIfUnknown(&ownership,
              701 + R127_SOURCE_OWNERSHIP_CAPACITY,
              R127SourceOwnerDirect) == R127SourceOwnerDirect &&
          R127SourceOwnershipRead(&ownership, 701) == R127SourceOwnerUnknown);
    check("unresolved FG slot refuses collision",
          R127SourceOwnershipSetIfUnknown(&ownership,
              702 + R127_SOURCE_OWNERSHIP_CAPACITY,
              R127SourceOwnerDirect) == R127SourceOwnerUnknown &&
          R127SourceOwnershipRead(&ownership, 702) == R127SourceOwnerSuppressed &&
          ownership.slotRefusals == 2 && ownership.overwriteNonTerminal == 0);
    check("FG dispatch transitions once",
          R127SourceOwnershipMarkDispatched(&ownership, 702) &&
          !R127SourceOwnershipMarkDispatched(&ownership, 702) &&
          R127SourceOwnershipRead(&ownership, 702) == R127SourceOwnerDispatched);
    check("terminal FG slot may be reused",
          R127SourceOwnershipSetIfUnknown(&ownership,
              702 + R127_SOURCE_OWNERSHIP_CAPACITY,
              R127SourceOwnerDirect) == R127SourceOwnerDirect);
    check("stale sequence cannot overwrite newer record",
          R127SourceOwnershipSetIfUnknown(&ownership, 702,
              R127SourceOwnerDirect) == R127SourceOwnerUnknown &&
          R127SourceOwnershipRead(&ownership,
              702 + R127_SOURCE_OWNERSHIP_CAPACITY) == R127SourceOwnerDirect);

    printf("R127 source fence and ownership self-test: %u cases, %u failures\n",
           cases, failures);
    return failures ? 1 : 0;
}
