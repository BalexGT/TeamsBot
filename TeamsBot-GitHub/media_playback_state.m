#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <dlfcn.h>

// MediaRemote is the system service behind Control Center's Now Playing card.
// This tiny local helper prints "playing", "paused", "stopped", or "unknown".
typedef void (*PlaybackStateReader)(dispatch_queue_t queue, void (^handler)(int state));

int main(void) {
    void *framework = dlopen("/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote", RTLD_LAZY);
    if (!framework) {
        puts("unknown");
        return 1;
    }
    PlaybackStateReader readState = (PlaybackStateReader)dlsym(framework, "MRMediaRemoteGetNowPlayingApplicationPlaybackState");
    if (!readState) {
        puts("unknown");
        dlclose(framework);
        return 1;
    }

    __block int state = 0;
    dispatch_semaphore_t finished = dispatch_semaphore_create(0);
    readState(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^(int value) {
        state = value;
        dispatch_semaphore_signal(finished);
    });
    long result = dispatch_semaphore_wait(finished, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));
    dlclose(framework);
    if (result != 0) {
        puts("unknown");
        return 1;
    }

    // MRPlaybackState: 1 playing, 2 paused, 3 stopped. Seeking is treated
    // as playing so a video cannot be interrupted while it is active.
    if (state == 1 || state == 5 || state == 6) puts("playing");
    else if (state == 2) puts("paused");
    else if (state == 3) puts("stopped");
    else puts("unknown");
    return 0;
}
