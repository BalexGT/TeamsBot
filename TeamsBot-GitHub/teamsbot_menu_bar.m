#import <Cocoa/Cocoa.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

@interface TeamsBotMenuDelegate : NSObject <NSApplicationDelegate, NSMenuDelegate>
@property(nonatomic, strong) NSStatusItem *statusItem;
@property(nonatomic, strong) NSImage *statusImage;
@end

static BOOL graphBetaBuild(void);

static void writeCommand(NSString *command) {
    NSString *folder = graphBetaBuild() ? @"TeamsBotGraphBeta" : @"TeamsBot";
    NSString *directory = [NSHomeDirectory() stringByAppendingPathComponent:[@"Library/Application Support/" stringByAppendingString:folder]];
    [[NSFileManager defaultManager] createDirectoryAtPath:directory withIntermediateDirectories:YES attributes:nil error:nil];
    NSString *path = [directory stringByAppendingPathComponent:@"menu-commands.jsonl"];
    NSDictionary *payload = @{ @"command": command, @"issued_at": @([[NSDate date] timeIntervalSince1970]) };
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    if (!data) return;
    NSMutableData *line = [data mutableCopy];
    [line appendData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        [[NSFileManager defaultManager] createFileAtPath:path contents:nil attributes:nil];
    }
    NSFileHandle *file = [NSFileHandle fileHandleForWritingAtPath:path];
    [file seekToEndOfFile];
    [file writeData:line];
    [file closeFile];
}

static int helperLock = -1;
static BOOL graphBetaEdition = NO;

static BOOL graphBetaBuild(void) {
    // PyInstaller places this helper under the app's Frameworks directory, so
    // NSBundle's identifier belongs to the helper rather than its parent app.
    // The executable path remains the reliable edition boundary.
    return graphBetaEdition;
}

static BOOL acquireSingleInstanceLock(void) {
    BOOL graphBeta = graphBetaBuild();
    NSString *folder = graphBeta ? @"TeamsBotGraphBeta" : @"TeamsBot";
    NSString *directory = [NSHomeDirectory() stringByAppendingPathComponent:[@"Library/Application Support/" stringByAppendingString:folder]];
    [[NSFileManager defaultManager] createDirectoryAtPath:directory withIntermediateDirectories:YES attributes:nil error:nil];
    NSString *path = [directory stringByAppendingPathComponent:(graphBeta ? @"menu-bar-graph-beta.lock" : @"menu-bar.lock")];
    helperLock = open(path.fileSystemRepresentation, O_CREAT | O_RDWR, 0600);
    return helperLock >= 0 && flock(helperLock, LOCK_EX | LOCK_NB) == 0;
}

@implementation TeamsBotMenuDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:28.0];
    NSString *imagePath = [[NSBundle mainBundle] pathForResource:@"TeamsBotMark" ofType:@"png"];
    self.statusImage = [[NSImage alloc] initWithContentsOfFile:imagePath];
    self.statusImage.size = NSMakeSize(20.0, 20.0);
    self.statusImage.template = NO;
    self.statusItem.button.image = self.statusImage;
    self.statusItem.button.imagePosition = NSImageOnly;
    BOOL graphBeta = graphBetaBuild();
    NSString *appName = graphBeta ? @"TeamsBot Graph Beta" : @"TeamsBot";
    self.statusItem.button.toolTip = appName;

    NSMenu *menu = [[NSMenu alloc] initWithTitle:appName];
    [self addItem:[@"Show " stringByAppendingString:appName] action:@selector(showWindow:) menu:menu];
    [self addItem:[@"Hide " stringByAppendingString:appName] action:@selector(hideWindow:) menu:menu];
    [menu addItem:[NSMenuItem separatorItem]];
    [self addItem:@"Start Monitoring" action:@selector(startMonitoring:) menu:menu];
    [self addItem:@"Stop Monitoring" action:@selector(stopMonitoring:) menu:menu];
    [menu addItem:[NSMenuItem separatorItem]];
    NSMenu *diagnosticsMenu = [[NSMenu alloc] initWithTitle:@"Diagnostics"];
    [self addItem:@"Diagnostics Guide…" action:@selector(diagnosticsGuide:) menu:diagnosticsMenu];
    [diagnosticsMenu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *safeTestItem = [self addItem:@"Safe Test Mode" action:@selector(toggleSafeTest:) menu:diagnosticsMenu];
    safeTestItem.representedObject = @"test_mode";
    NSMenuItem *technicalItem = [self addItem:@"Technical Details" action:@selector(toggleTechnicalDetails:) menu:diagnosticsMenu];
    technicalItem.representedObject = @"technical_details";
    [diagnosticsMenu addItem:[NSMenuItem separatorItem]];
    [self addItem:@"Inspect Current Screen" action:@selector(inspectCurrent:) menu:diagnosticsMenu];
    [self addItem:@"Test Newest Submit" action:@selector(testNewestSubmit:) menu:diagnosticsMenu];
    [self addItem:@"Scan Visible Timestamps" action:@selector(scanTimestamps:) menu:diagnosticsMenu];
    NSMenuItem *continuousItem = [self addItem:@"Continuous Timestamp Scan" action:@selector(toggleContinuousScan:) menu:diagnosticsMenu];
    continuousItem.representedObject = @"continuous_timestamp_scan";
    [diagnosticsMenu addItem:[NSMenuItem separatorItem]];
    [self addItem:@"Verify & Repair Scan State" action:@selector(repairScanState:) menu:diagnosticsMenu];
    [self addItem:@"Start New Session…" action:@selector(newSession:) menu:diagnosticsMenu];
    [self addItem:@"Poll History…" action:@selector(pollHistory:) menu:diagnosticsMenu];
    NSMenuItem *diagnosticsItem = [[NSMenuItem alloc] initWithTitle:@"Diagnostics" action:nil keyEquivalent:@""];
    [menu addItem:diagnosticsItem];
    [menu setSubmenu:diagnosticsMenu forItem:diagnosticsItem];
    diagnosticsMenu.delegate = self;
    [menu addItem:[NSMenuItem separatorItem]];
    [self addItem:[@"Quit " stringByAppendingString:appName] action:@selector(quitApp:) menu:menu];
    self.statusItem.menu = menu;
}

- (NSMenuItem *)addItem:(NSString *)title action:(SEL)action menu:(NSMenu *)menu {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:action keyEquivalent:@""];
    item.target = self;
    [menu addItem:item];
    return item;
}

- (void)menuNeedsUpdate:(NSMenu *)menu {
    NSString *folder = graphBetaBuild() ? @"TeamsBotGraphBeta" : @"TeamsBot";
    NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:[@"Library/Application Support/" stringByAppendingPathComponent:[folder stringByAppendingPathComponent:@"shortcuts-status.json"]]];
    NSData *data = [NSData dataWithContentsOfFile:path];
    NSDictionary *status = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    for (NSMenuItem *item in menu.itemArray) {
        NSString *key = [item.representedObject isKindOfClass:[NSString class]] ? item.representedObject : nil;
        if (key) item.state = [status[key] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
    }
}

- (void)showWindow:(id)sender { writeCommand(@"show"); }
- (void)hideWindow:(id)sender { writeCommand(@"hide"); }
- (void)diagnosticsGuide:(id)sender { writeCommand(@"diagnostics_guide"); }
- (void)toggleSafeTest:(id)sender { writeCommand(@"toggle_safe_test"); }
- (void)toggleTechnicalDetails:(id)sender { writeCommand(@"toggle_technical_details"); }
- (void)inspectCurrent:(id)sender { writeCommand(@"inspect_current"); }
- (void)testNewestSubmit:(id)sender { writeCommand(@"test_newest_submit"); }
- (void)scanTimestamps:(id)sender { writeCommand(@"scan_timestamps"); }
- (void)toggleContinuousScan:(id)sender { writeCommand(@"toggle_continuous_scan"); }
- (void)repairScanState:(id)sender { writeCommand(@"repair_scan_state"); }
- (void)newSession:(id)sender { writeCommand(@"new_session"); }
- (void)pollHistory:(id)sender { writeCommand(@"poll_history"); }
- (void)startMonitoring:(id)sender { writeCommand(@"start"); }
- (void)stopMonitoring:(id)sender { writeCommand(@"stop"); }
- (void)quitApp:(id)sender { writeCommand(@"quit"); [NSApp terminate:nil]; }
@end

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSString *executablePath = [[NSString alloc] initWithUTF8String:argv[0] ?: ""];
        graphBetaEdition = [[executablePath lowercaseString] containsString:@"teamsbotgraphbeta.app"];
        // The lock stays open for this process lifetime; a second app launch
        // therefore cannot add another status-bar icon.
        if (!acquireSingleInstanceLock()) return 0;
        NSApplication *app = [NSApplication sharedApplication];
        TeamsBotMenuDelegate *delegate = [TeamsBotMenuDelegate new];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
