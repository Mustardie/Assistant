/**
 * Nova Premiere Bridge - ExtendScript host entry point.
 *
 * The panel calls exactly one function: NovaBridge.dispatch(jsonString). That
 * single entry point is deliberate -- it is the reason the editing model can
 * never execute arbitrary code inside Premiere. Requests are data; only the
 * operations in the table below can run, and each one receives already-typed
 * parameters that were validated on the Python side before they were sent.
 *
 * Load order matters: lib/json defines JSON for ES3, lib/util is used by every
 * module, and the modules reference each other (clips -> project, effects ->
 * params, media -> clips) so they follow in dependency order.
 */

#include "lib/json.jsx"
#include "lib/util.jsx"
#include "modules/project.jsx"
#include "modules/timeline.jsx"
#include "modules/params.jsx"
#include "modules/clips.jsx"
#include "modules/effects.jsx"
#include "modules/media.jsx"
#include "modules/markers.jsx"
#include "modules/transcript.jsx"
#include "modules/caps.jsx"

var NovaBridge = (function () {
    var U = NovaUtil;

    /**
     * The complete set of operations this host will perform.
     *
     * Names match the Python catalog one-for-one where the operation is a
     * primitive. High-level catalog operations (animate, audio.duck,
     * text.create, captions.build...) are expanded into these by the engine
     * before they arrive, which keeps the maths in testable Python and this
     * side a thin, auditable executor.
     */
    var OPS = {
        // -- project -----------------------------------------------------
        'project.info':      function (p) { return NovaProject.info(p); },
        'project.path':      function (p) { return NovaProject.path(p); },
        'project.save':      function (p) { return NovaProject.save(p); },
        'project.open':      function (p) { return NovaProject.open(p); },
        'project.assets':    function (p) { return NovaProject.assets(p); },
        'project.import':    function (p) { return NovaProject.importFiles(p); },
        'project.bin':       function (p) { return NovaProject.createBin(p); },

        // -- sequences ---------------------------------------------------
        'sequence.list':     function (p) { return NovaProject.listSequences(p); },
        'sequence.info':     function (p) { return NovaProject.sequenceInfo(p); },
        'sequence.activate': function (p) { return NovaProject.activateSequence(p); },
        'sequence.create':   function (p) { return NovaProject.createSequence(p); },

        // -- timeline ----------------------------------------------------
        'timeline.snapshot': function (p) { return NovaTimeline.snapshot(p); },
        'timeline.revision': function (p) { return NovaTimeline.revisionOf(null); },
        'timeline.playhead': function (p) { return NovaTimeline.playhead(p); },
        'track.add':         function (p) { return NovaTimeline.addTracks(p); },
        'track.set':         function (p) { return NovaTimeline.setTrack(p); },
        'track.remove':      function (p) { return NovaTimeline.removeTrack(p); },

        // -- clips -------------------------------------------------------
        'clip.resolve':      function (p) { return NovaClips.resolveOp(p); },
        'clip.insert':       function (p) { return NovaClips.insert(p); },
        'clip.overwrite':    function (p) { return NovaClips.overwrite(p); },
        'clip.append':       function (p) { return NovaClips.append(p); },
        'clip.remove':       function (p) { return NovaClips.remove(p); },
        'clip.move':         function (p) { return NovaClips.move(p); },
        'clip.duplicate':    function (p) { return NovaClips.duplicate(p); },
        'clip.split':        function (p) { return NovaClips.split(p); },
        'clip.trim':         function (p) { return NovaClips.trim(p); },
        'clip.slip':         function (p) { return NovaClips.slip(p); },
        'clip.set':          function (p) { return NovaClips.setFlags(p); },
        'clip.speed':        function (p) { return NovaClips.speed(p); },
        'clip.freeze':       function (p) { return NovaClips.freeze(p); },
        'clip.link':         function (p) { return NovaClips.link(p); },
        'clip.replace_source': function (p) { return NovaClips.replaceSource(p); },
        'gap.remove':        function (p) { return NovaClips.gapRemove(p); },

        // -- properties and keyframes (the generic core) -----------------
        'property.get':      function (p) { return NovaParams.get(p); },
        'property.set':      function (p) { return NovaParams.set(p); },
        'property.reset':    function (p) { return NovaParams.reset(p); },
        'keyframe.list':     function (p) { return NovaParams.listKeys(p); },
        'keyframe.set':      function (p) { return NovaParams.setKeys(p); },
        'keyframe.add':      function (p) { return NovaParams.addKey(p); },
        'keyframe.remove':   function (p) { return NovaParams.removeKeys(p); },
        'keyframe.clear':    function (p) { return NovaParams.clear(p); },
        'keyframe.move':     function (p) { return NovaParams.moveKeys(p); },
        'keyframe.interpolation':
                             function (p) { return NovaParams.setInterpolation(p); },

        // -- effects and transitions -------------------------------------
        'effect.list':       function (p) { return NovaEffects.list(p); },
        'effect.apply':      function (p) { return NovaEffects.apply(p); },
        'effect.remove':     function (p) { return NovaEffects.remove(p); },
        'transition.apply':  function (p) { return NovaEffects.applyTransition(p); },
        'transition.remove': function (p) { return NovaEffects.removeTransition(p); },
        'color.lut':         function (p) { return NovaEffects.applyLut(p); },

        // -- media, graphics, captions -----------------------------------
        'overlay.place':     function (p) { return NovaMedia.place(p); },
        'frame.export':      function (p) { return NovaMedia.exportFrame(p); },
        'mogrt.import':      function (p) { return NovaMedia.importMGT(p); },
        'mogrt.params':      function (p) { return NovaMedia.listMGTParams(p); },
        'captions.import':   function (p) { return NovaMedia.importCaptions(p); },
        'adjustment.create': function (p) { return NovaMedia.createAdjustment(p); },

        // -- markers and attribute copying -------------------------------
        'marker.list':       function (p) { return NovaMarkers.list(p); },
        'marker.add':        function (p) { return NovaMarkers.add(p); },
        'marker.remove':     function (p) { return NovaMarkers.remove(p); },
        'clip.copy_attributes':
                             function (p) { return NovaMarkers.copyAttributes(p); },

        // -- transcript (read-only; Speech to Text / Transcript panel) ----
        'transcript.caps':   function (p) { return NovaTranscript.caps(p); },
        'transcript.read':   function (p) { return NovaTranscript.read(p); },

        // -- capability discovery ----------------------------------------
        'caps.probe':        function (p) { return NovaCaps.probe(p); },
        'caps.params':       function (p) { return NovaCaps.params(p); },
        'caps.effects':      function (p) { return NovaEffects.listEffects(p); },
        'caps.transitions':  function (p) { return NovaEffects.listTransitions(p); }
    };

    function knownOps() {
        var names = [];
        for (var name in OPS) {
            if (Object.prototype.hasOwnProperty.call(OPS, name)) {
                names[names.length] = name;
            }
        }
        names.sort();
        return names;
    }

    /**
     * Execute one request.
     *
     * Always returns a JSON string, never throws across the boundary -- the
     * panel has no way to catch an ExtendScript exception, so an uncaught
     * throw here would surface to Python as an empty response with no
     * explanation of what went wrong.
     */
    function dispatch(payload) {
        var request;
        try {
            request = JSON.parse(payload);
        } catch (e) {
            return JSON.stringify({
                ok: false,
                error: { code: 'bad_request',
                         message: 'Host could not parse the request: ' + e }
            });
        }

        var op = request.op;
        var handler = OPS[op];
        if (!handler) {
            return JSON.stringify({
                ok: false,
                error: {
                    code: 'unknown_operation',
                    message: 'Unknown host operation "' + op + '"',
                    hint: 'The panel and the Python layer may be different '
                        + 'versions. Reinstall the extension.',
                    detail: { known: knownOps() }
                }
            });
        }

        if (!app || !app.project) {
            return JSON.stringify({
                ok: false,
                error: { code: 'no_project',
                         message: 'No project is open in Premiere',
                         hint: 'Open a project, then retry.' }
            });
        }

        try {
            var result = handler(request.params || {});
            return JSON.stringify({ ok: true, result: (result === undefined)
                                                      ? null : result });
        } catch (error) {
            // Errors raised through NovaUtil.fail carry structure; anything
            // else is an unexpected ExtendScript throw and gets wrapped with
            // enough context to be debuggable from the Python side.
            var structured = error && error.novaError;
            if (structured) {
                return JSON.stringify({ ok: false, error: structured });
            }
            return JSON.stringify({
                ok: false,
                error: {
                    code: 'host_error',
                    message: String(error && error.message ? error.message : error),
                    detail: {
                        op: op,
                        line: (error && error.line) ? error.line : undefined,
                        file: (error && error.fileName) ? String(error.fileName)
                                                        : undefined
                    }
                }
            });
        }
    }

    return {
        dispatch: dispatch,
        ops: knownOps
    };
}());
