// MediaRemote adapter: reports what macOS considers to be playing.
//
// Prints the active player's metadata as JSON - title, artist, album, duration,
// elapsed time and playback rate - either once or as a stream that emits on
// change.
//
// Since macOS 15.4 MRMediaRemoteGetNowPlayingInfo inspects the calling binary
// and hands a nil dictionary to anything that is not an Apple platform binary.
// This therefore ships as a dylib loaded by /usr/bin/perl, so perl is the
// process being inspected. No entitlement, signing or SIP change needed.
//
// Nothing blocks in the constructor: dyld holds the loader lock while it runs
// and the XPC reply path can itself need to load code. The constructor arms the
// callbacks and returns; perl's sleep keeps the process alive to receive them.

#import <Foundation/Foundation.h>
#import <dlfcn.h>

typedef void (*MRGetInfo)(dispatch_queue_t, void (^)(CFDictionaryRef));
typedef void (*MRGetClient)(dispatch_queue_t, void (^)(id));
typedef void (*MRGetClients)(dispatch_queue_t, void (^)(NSArray *));
typedef void (*MRRegister)(dispatch_queue_t);
typedef void (*MRGetIsPlaying)(dispatch_queue_t, void (^)(Boolean));

static MRGetInfo      gGetInfo;
static MRGetClient    gGetClient;
static MRGetClients   gGetClients;
static MRGetIsPlaying gIsPlaying;

// Read an MRClient through its ObjC accessors, not the C
// MRNowPlayingClientGet* functions - those segfault on a non-active client.
static NSString *clientStr(id c, NSString *key) {
    if (!c) return nil;
    @try {
        id v = [c valueForKey:key];
        return [v isKindOfClass:NSString.class] ? v : nil;
    } @catch (NSException *e) { return nil; }
}

static dispatch_queue_t gQ;
static BOOL   gStream;          // keep running and report changes
static BOOL   gWantArt;         // include base64 artwork
static NSString *gLastIdentity; // suppress re-emitting an unchanged track

// Track identity. Elapsed time and info age are excluded: they are republished
// about once a second and would emit a change every time.
static NSString *identity(NSDictionary *d) {
    return [NSString stringWithFormat:@"%@|%@|%@|%@|%@|%@",
            d[@"bundleId"] ?: @"", d[@"title"] ?: @"", d[@"artist"] ?: @"",
            d[@"album"] ?: @"", d[@"artworkId"] ?: @"", d[@"state"] ?: @""];
}

// NSJSONSerialization throws on a non-finite double. Live streams report an
// infinite duration, so guard every number.
static NSNumber *finite(id v) {
    if (![v isKindOfClass:NSNumber.class]) return nil;
    double x = [v doubleValue];
    return isfinite(x) ? @(x) : nil;
}

static void emit(NSDictionary *d) {
    NSString *id_ = identity(d);
    if (gStream && gLastIdentity && [gLastIdentity isEqualToString:id_]) return;
    gLastIdentity = id_;
    NSData *j = nil;
    @try {
        if ([NSJSONSerialization isValidJSONObject:d]) {
            NSError *e = nil;
            j = [NSJSONSerialization dataWithJSONObject:d options:0 error:&e];
        }
    } @catch (NSException *ex) {
        fprintf(stderr, "json: %s\n", ex.reason.UTF8String);
    }
    if (!j) return;
    NSString *s = [[NSString alloc] initWithData:j encoding:NSUTF8StringEncoding];
    fprintf(stdout, "%s\n", s.UTF8String);
    fflush(stdout);
    if (!gStream) exit(0);
}

// Collect the three async answers (info, client, isPlaying) then emit once.
static void snapshot(void) {
    __block NSMutableDictionary *out = [NSMutableDictionary dictionary];
    dispatch_group_t g = dispatch_group_create();

    dispatch_group_enter(g);
    gGetInfo(gQ, ^(CFDictionaryRef info) {
        NSDictionary *d = (__bridge NSDictionary *)info;
        if (d.count) {
            NSString *t = d[@"kMRMediaRemoteNowPlayingInfoTitle"];
            NSString *a = d[@"kMRMediaRemoteNowPlayingInfoArtist"];
            NSString *al = d[@"kMRMediaRemoteNowPlayingInfoAlbum"];
            if (t) out[@"title"] = t;
            if (a) out[@"artist"] = a;
            if (al) out[@"album"] = al;
            NSNumber *dur = finite(d[@"kMRMediaRemoteNowPlayingInfoDuration"]);
            NSNumber *el  = finite(d[@"kMRMediaRemoteNowPlayingInfoElapsedTime"]);
            NSNumber *rt  = finite(d[@"kMRMediaRemoteNowPlayingInfoPlaybackRate"]);
            if (dur) out[@"duration"] = dur;
            if (el)  out[@"elapsed"]  = el;
            if (rt)  out[@"rate"]     = rt;
            // ContentItemIdentifier is deliberately not reported: it is
            // regenerated on every query. ArtworkIdentifier is the stable one.
            id aid = d[@"kMRMediaRemoteNowPlayingInfoArtworkIdentifier"];
            if (aid) out[@"artworkId"] = [aid description];
            // The timestamp is when the player last published, which is how we
            // extrapolate elapsed time without re-querying.
            NSDate *ts = d[@"kMRMediaRemoteNowPlayingInfoTimestamp"];
            if ([ts isKindOfClass:NSDate.class]) {
                NSNumber *age = finite(@(-[ts timeIntervalSinceNow]));
                if (age) out[@"infoAge"] = age;
            }
            if (gWantArt) {
                NSData *art = d[@"kMRMediaRemoteNowPlayingInfoArtworkData"];
                if ([art isKindOfClass:NSData.class] && art.length)
                    out[@"artwork"] = [art base64EncodedStringWithOptions:0];
            }
        }
        dispatch_group_leave(g);
    });

    if (gGetClient) {
        dispatch_group_enter(g);
        gGetClient(gQ, ^(id client) {
            if (client) {
                NSString *b = clientStr(client, @"bundleIdentifier");
                if (!b) b = clientStr(client, @"parentApplicationBundleIdentifier");
                NSString *n = clientStr(client, @"displayName");
                if (b) out[@"bundleId"] = b;
                if (n) out[@"appName"] = n;
            }
            dispatch_group_leave(g);
        });
    }

    if (gGetClients) {
        dispatch_group_enter(g);
        gGetClients(gQ, ^(NSArray *cs) {
            NSMutableArray *ps = [NSMutableArray array];
            for (id c in cs) {
                NSString *b = clientStr(c, @"bundleIdentifier");
                NSString *n = clientStr(c, @"displayName");
                if (b || n) [ps addObject:@{@"bundleId": b ?: @"",
                                            @"name": n ?: @""}];
            }
            if (ps.count) out[@"players"] = ps;
            dispatch_group_leave(g);
        });
    }

    if (gIsPlaying) {
        dispatch_group_enter(g);
        gIsPlaying(gQ, ^(Boolean playing) {
            out[@"playing"] = playing ? @YES : @NO;
            dispatch_group_leave(g);
        });
    }

    dispatch_group_notify(g, gQ, ^{
        // Fall back to the rate when the dedicated query is unavailable.
        if (!out[@"playing"] && out[@"rate"])
            out[@"playing"] = [out[@"rate"] doubleValue] > 0 ? @YES : @NO;
        if (!out[@"title"]) { out[@"state"] = @"none"; }
        else out[@"state"] = [out[@"playing"] boolValue] ? @"playing" : @"paused";
        emit(out);
    });
}

// MediaRemote fires several notifications per track change; coalesce them.
static dispatch_source_t gDebounce;
static dispatch_source_t gTick;
static void schedule(void) {
    if (gDebounce) dispatch_source_cancel(gDebounce);
    gDebounce = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, gQ);
    dispatch_source_set_timer(gDebounce,
        dispatch_time(DISPATCH_TIME_NOW, 350ull * NSEC_PER_MSEC),
        DISPATCH_TIME_FOREVER, 50ull * NSEC_PER_MSEC);
    dispatch_source_set_event_handler(gDebounce, ^{
        dispatch_source_cancel(gDebounce); gDebounce = nil; snapshot();
    });
    dispatch_resume(gDebounce);
}

__attribute__((constructor))
static void start(void) {
    void *h = dlopen("/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote",
                     RTLD_NOW);
    if (!h) { fprintf(stderr, "{\"error\":\"MediaRemote unavailable\"}\n"); exit(3); }
    gGetInfo        = (MRGetInfo)dlsym(h, "MRMediaRemoteGetNowPlayingInfo");
    gGetClient      = (MRGetClient)dlsym(h, "MRMediaRemoteGetNowPlayingClient");
    gGetClients     = (MRGetClients)dlsym(h, "MRMediaRemoteGetNowPlayingClients");
    gIsPlaying      = (MRGetIsPlaying)dlsym(h, "MRMediaRemoteGetNowPlayingApplicationIsPlaying");
    if (!gGetInfo) { fprintf(stderr, "{\"error\":\"no MRMediaRemoteGetNowPlayingInfo\"}\n"); exit(3); }

    const char *m = getenv("MRA_MODE");
    gStream  = (m && !strcmp(m, "stream"));
    gWantArt = getenv("MRA_ARTWORK") != NULL;
    gQ = dispatch_queue_create("mradapter", DISPATCH_QUEUE_SERIAL);

    if (gStream) {
        MRRegister reg = (MRRegister)dlsym(h, "MRMediaRemoteRegisterForNowPlayingNotifications");
        if (reg) reg(gQ);
        NSNotificationCenter *nc = NSNotificationCenter.defaultCenter;
        for (NSString *n in @[@"kMRMediaRemoteNowPlayingInfoDidChangeNotification",
                              @"kMRMediaRemoteNowPlayingApplicationDidChangeNotification",
                              @"kMRMediaRemoteNowPlayingApplicationIsPlayingDidChangeNotification"])
            [nc addObserverForName:n object:nil queue:nil
                        usingBlock:^(NSNotification *_) { schedule(); }];
        // Safety net in case a notification is missed. gTick must be static:
        // as a local, ARC releases it when the constructor returns and the
        // source is destroyed before it ever fires.
        gTick = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, gQ);
        dispatch_source_set_timer(gTick, dispatch_time(DISPATCH_TIME_NOW, 0),
                                  10ull * NSEC_PER_SEC, NSEC_PER_SEC);
        dispatch_source_set_event_handler(gTick, ^{ snapshot(); });
        dispatch_resume(gTick);
        snapshot();   // never wait on a notification for the first answer
    } else {
        snapshot();
        // One-shot: never hang a caller if MediaRemote does not answer.
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 5ull * NSEC_PER_SEC), gQ, ^{
            fprintf(stdout, "{\"state\":\"timeout\"}\n"); fflush(stdout); exit(4);
        });
    }
}
