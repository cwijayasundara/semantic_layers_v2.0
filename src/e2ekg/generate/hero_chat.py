"""The hero incident channel's story beats, written like a real incident room.

(h, m, s, who, text, reply_to). `who` is "lead", "twin" (TWIN_RESOLVER) or an
index into the channel roster; `reply_to` is the index of an earlier beat to
thread under. Wrong guesses (api-gateway deploy, android release, cell B) are
walked back; "bump min to 40" comes before anyone names the property change.
No change ids or answer-key wording appear. Fillers top the channel up to
exactly HERO_MESSAGES."""

HERO_MESSAGES, HERO_USERS = 200, 50
# Exactly the services the hero channel mentions (by id or nickname).
HERO_SERVICES = ("title-metadata-svc", "title-batch-loader", "row-ranker", "lolomo-svc",
                 "homepage-graphql", "edge-gateway", "api-gateway", "client-android",
                 "client-tv", "client-ios", "client-web", "artwork-svc", "ab-allocator",
                 "evcache-titles", "profile-svc")

BEATS: tuple[tuple[int, int, int, object, str, int | None], ...] = (
    (15, 34, 20, "lead", "Opening INC-5377 for a homepage TTR regression. Radar fired at 15:30 on "
     "homepage-graphql, TTR p90 is up across the board. Who's around from Discovery?", None),
    (15, 35, 5, 1, "here. homepage-graphql p99 is up and we're seeing partial responses on the "
     "lolomo field", None),
    (15, 35, 40, 2, "client side: client-tv and client-android both regressed starting around "
     "15:20",
     None),
    (15, 36, 30, 3, "api-gateway deployed at 15:18, that lines up pretty well. can we roll it "
     "back?",
     None),
    (15, 37, 0, 4, "looking. api-gateway had a short error blip right after the deploy, looks like "
     "warm-up noise but checking", 3),
    (15, 37, 45, "lead", "marking api-gateway as the affected service for now until we know more",
     None),
    (15, 38, 30, 5, "also: android 8.112.0 went to 100% at 15:12. could this be the client-android "
     "release?", None),
    (15, 39, 10, 6, "row-ranker cell B experiment went to 5% at 15:05 too. lots of candidates",
     None),
    (15, 40, 0, 7, "edge-gateway started shedding a little around 15:21, 503s on /graphql", None),
    (15, 41, 15, 2, "client-ios and client-web are up too. all four platforms, roughly the same "
     "delta", None),
    (15, 42, 0, 5, "if web and tv regress by the same amount it's not the client-android release. "
     "dropping that one", 6),
    (15, 43, 20, 4, "starting a rollback of api-gateway to the previous server group, ~10 min", 3),
    (15, 44, 10, 8, "lolomo-svc traces are slow, 1.2s+ on GetLolomo, mostly waiting on the ranker",
     None),
    (15, 45, 0, 9, "the ranker is fine on CPU, but its calls into title-batch-loader are hitting "
     "the deadline", 12),
    (15, 46, 30, 6, "checked ab-allocator and the TTR split: cell A and cell B regressed by the "
     "same "
     "amount. not the experiment", 7),
    (15, 47, 10, 10, "artwork-svc is throwing NullPointerExceptions, is that new?", None),
    (15, 48, 0, 11, "nah, that artwork-svc NPE has been there all week, there's a ticket. red "
     "herring", 15),
    (15, 49, 30, 12, "title-batch-loader logs are full of Connection pool exhausted (active=64, "
     "idle=0, waiting=...) against title-metadata-svc", None),
    (15, 50, 15, 9, "so the batch loader is starved on its pool to TMS and everything above it "
     "queues", 17),
    (15, 51, 0, 13, "title-metadata-svc p99 went from ~40ms to 120ms+ around 15:05", None),
    (15, 52, 20, "lead", "so far: TMS slow -> title-batch-loader pool exhausted -> row-ranker "
     "deadlines -> lolomo-svc slow -> homepage-graphql partial responses. what made TMS slow?",
     None),
    (15, 53, 0, 14, "evcache-titles hit rate looks normal, not a cache problem", 20),
    (15, 54, 10, 12, "TMS instance count is way down. the ASG scaled in from 56 over about ten "
     "minutes", 20),
    (15, 55, 0, 15, "why would it scale in right as evening traffic ramps?", 22),
    (15, 56, 30, 4, "api-gateway rollback finished and TTR didn't move. scratch that, it's not "
     "api-gateway", 3),
    (15, 57, 10, "twin", "this looks a lot like {twin} from last quarter ({twin_channel}): "
     "home rows "
     "went empty with the same pool exhaustion shape. worth a read before we go further", None),
    (15, 58, 0, 12, "TMS CPU per instance is pegged. it's not slow code, there just aren't enough "
     "boxes", None),
    (16, 0, 30, 13, "can we bump min to 40 on the TMS ASG and let it scale back out?", None),
    (16, 1, 10, "lead", "yes, do it. bump min to 40 now, we'll figure out why after", 27),
    (16, 2, 0, 12, "bump min to 40 submitted on the ASG, instances launching", 27),
    (16, 5, 0, 16, "comms posted a holding statement for members on all four platforms", None),
    (16, 6, 0, 12, "hm. desired went up to 40 and then right back down within a minute. "
                   "something keeps resetting it", 27),
    (16, 8, 30, 17, "is a scaling policy fighting us? the TargetTracking low alarm keeps firing",
     31),
    (16, 10, 0, 13, "ASG activity history shows six scale-ins for TMS between 15:00 and 15:10",
     31),
    (16, 14, 0, "lead", "updated the incident: affected service is homepage-graphql, not "
     "api-gateway", None),
    (16, 16, 0, 18, "homepage-graphql error budget burn is at 14x, we'll blow the monthly budget "
     "if this keeps up", None),
    (16, 20, 0, 9, "the ranker deadline to title-batch-loader is 500ms. we could raise it but that "
     "just moves the queue", None),
    (16, 24, 0, 1, "lolomo-svc is serving fallback rows for a few percent of requests", None),
    (16, 27, 0, 19, "profile-svc and artwork-svc look healthy, ruling them out", None),
    (16, 31, 0, 12, "tried to bump min to 40 again, same thing, back down in under a minute", 31),
    (16, 36, 0, 20, "the Archaius poller on TMS refreshes every 60s. could the floor be a fast "
     "property?", 31),
    (16, 41, 0, 12, "found it I think: the title-metadata-svc.asg.minInstances fast property was "
     "changed from 48 to 12 at 14:52. the poller keeps putting the ASG back to it", 31),
    (16, 42, 30, 13, "that explains the scale-in starting at 15:00 and the pool exhaustion at "
     "15:08", 41),
    (16, 43, 0, "lead", "great find. who made the change and was it meant for prod?", 41),
    (16, 46, 0, 21, "it came from a cost-savings ticket. it was meant for the test stack, not "
     "prod", 43),
    (16, 50, 0, "lead", "plan: put minInstances back from 12 to 48, then let the ASG scale out. "
     "need the Content Platform owner to approve a prod fast property change", None),
    (16, 55, 0, 22, "paging the Content Platform owner for approval", 45),
    (17, 5, 0, 14, "if we scale TMS out fast the cold instances will hammer evcache-titles, want a "
     "warm-up plan first", 45),
    (17, 20, 0, 12, "owner approved. revert is staged, applying at 17:55 with the warm-up plan",
     45),
    (17, 30, 0, 14, "evcache-titles has headroom, warm-up plan looks fine", 47),
    (17, 45, 0, "lead", "status: TMS capacity was cut by a fast property, revert going out "
     "shortly, TTR still elevated on all four platforms", None),
    (17, 52, 0, 16, "comms updated", None),
)

FILLERS = (
    "+1, seeing it on {plat} as well", "joining late, catching up", "any update?",
    "watching the {svc} dashboards", "{quiet} looks clean from our side",
    "customer service is getting slow home screen reports", ":eyes:",
    "is there a doc for this?", "ack", "following", "do we need a status page update?",
    "I can help with comms", "pulling traces for {svc}", "nothing weird in {quiet} logs",
    "should we page Content Platform?", "adding the Discovery on-call",
    "is this region specific?", "both regions look affected to me",
    "{plat} QA confirms slow home rows", "lurking, ping me if you need hands",
    "which dashboard are we all looking at?", "same here", "thanks for driving",
    "can someone post the graph link?",
)
FILLER_PLATFORMS = ("tv", "android", "ios", "web")
FILLER_SVC = ("homepage-graphql", "edge-gateway", "client-web", "client-tv", "profile-svc")
FILLER_QUIET = ("profile-svc", "artwork-svc", "evcache-titles")
