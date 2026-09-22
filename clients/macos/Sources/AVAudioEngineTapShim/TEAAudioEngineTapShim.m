#import "TEAAudioEngineTapShim.h"

NSString *const TEAAudioEngineTapShimErrorDomain =
    @"com.tea-asr.audio-engine-tap-shim";

BOOL TEAInstallAudioInputTap(
    AVAudioInputNode *node,
    AVAudioFrameCount bufferSize,
    AVAudioFormat *format,
    AVAudioNodeTapBlock block,
    NSError **error
) {
    @try {
        [node installTapOnBus:0 bufferSize:bufferSize format:format block:block];
        return YES;
    } @catch (NSException *exception) {
        if (error != NULL) {
            NSString *reason = exception.reason ?: exception.name;
            *error = [NSError errorWithDomain:TEAAudioEngineTapShimErrorDomain
                                          code:1
                                      userInfo:@{
                                          NSLocalizedDescriptionKey:
                                              [NSString stringWithFormat:
                                                  @"AVFAudio 無法安裝輸入 tap：%@", reason]
                                      }];
        }
        return NO;
    }
}
