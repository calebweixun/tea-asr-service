#import <AVFAudio/AVFAudio.h>

NS_ASSUME_NONNULL_BEGIN

FOUNDATION_EXPORT NSString *const TEAAudioEngineTapShimErrorDomain;

/// Installs exactly one AVAudioEngine input-node tap and translates the
/// Objective-C exception AVFAudio may raise into an NSError.
FOUNDATION_EXPORT BOOL TEAInstallAudioInputTap(
    AVAudioInputNode *node,
    AVAudioFrameCount bufferSize,
    AVAudioFormat *_Nullable format,
    AVAudioNodeTapBlock block,
    NSError *_Nullable *_Nullable error
);

NS_ASSUME_NONNULL_END
