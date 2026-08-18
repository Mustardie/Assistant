/**
 * Project panel: media discovery, import, bins, sequences, save/open.
 *
 * Asset discovery matters more than it looks. An autonomous editor cannot edit
 * footage it cannot name, and it must not invent filenames -- so
 * `project.assets` returns the real inventory (with durations, frame rates and
 * resolutions) and `findAsset` accepts any of the ways a model might refer to
 * one: exact name, path, basename, or a loose match.
 */

var NovaProject = (function () {
    var U = NovaUtil;

    // ------------------------------------------------------------------
    // Info
    // ------------------------------------------------------------------

    function info() {
        var project = app.project;
        if (!project) {
            return { name: null, note: 'No project is open in Premiere.' };
        }
        var out = {
            name: String(project.name),
            path: String(project.path || ''),
            version: String(app.version),
            build: safe(function () { return String(app.build); }),
            sequences: [],
            sequence: null
        };
        try {
            for (var i = 0; i < project.sequences.numSequences; i++) {
                var sequence = project.sequences[i];
                out.sequences[out.sequences.length] = {
                    name: String(sequence.name),
                    id: String(sequence.sequenceID)
                };
            }
        } catch (e) { /* sequences unreadable */ }
        try {
            if (project.activeSequence) {
                out.sequence = String(project.activeSequence.name);
            }
        } catch (e2) { /* none active */ }
        return out;
    }

    function safe(fn, fallback) {
        try { return fn(); } catch (e) { return fallback; }
    }

    function path() {
        if (!app.project) { throw U.fail('No project is open'); }
        return { path: String(app.project.path || ''),
                 name: String(app.project.name) };
    }

    function save(params) {
        if (!app.project) { throw U.fail('No project is open'); }
        if (params && params.path) {
            app.project.saveAs(String(params.path));
            return { saved: true, path: String(params.path) };
        }
        if (!app.project.path) {
            throw U.fail('This project has never been saved', {
                hint: 'Save it once in Premiere so it has a path on disk'
            });
        }
        app.project.save();
        return { saved: true, path: String(app.project.path) };
    }

    function open(params) {
        var target = params && params.path;
        if (!target) { throw U.fail('project.open needs a path'); }
        app.openDocument(String(target));
        return { opened: String(target) };
    }

    // ------------------------------------------------------------------
    // Assets
    // ------------------------------------------------------------------

    function walk(item, out, options, binName, depth) {
        if (depth > 12) { return; }   // guard against pathological nesting
        for (var i = 0; i < item.children.numItems; i++) {
            var child = item.children[i];
            var kind = safe(function () { return child.type; }, 0);

            // ProjectItemType: 1 = CLIP, 2 = BIN, 3 = ROOT, 4 = FILE
            if (kind === 2) {
                if (options.recursive) {
                    walk(child, out, options, String(child.name), depth + 1);
                }
                continue;
            }

            var entry = describeAsset(child, binName);
            if (options.media_type && options.media_type !== 'any' &&
                entry.media_type !== options.media_type) {
                continue;
            }
            out[out.length] = entry;
        }
    }

    function describeAsset(item, binName) {
        var entry = {
            name: String(item.name),
            bin: binName || '',
            media_type: 'unknown'
        };
        entry.path = safe(function () { return String(item.getMediaPath()); }, '');

        var isSequence = safe(function () { return item.isSequence(); }, false);
        if (isSequence) { entry.media_type = 'sequence'; }

        try {
            var meta = item.getProjectMetadata();
            entry.duration = readDuration(item);
            if (!isSequence) {
                entry.media_type = guessType(entry.path, item, meta);
            }
        } catch (e) {
            if (!isSequence) { entry.media_type = guessType(entry.path, item, ''); }
        }

        // Frame rate and resolution come from the footage interpretation, which
        // is what the editor needs to plan scaling and speed changes.
        try {
            var interpretation = item.getFootageInterpretation();
            if (interpretation) {
                if (interpretation.frameRate) {
                    entry.fps = Number(interpretation.frameRate);
                }
                if (interpretation.pixelAspectRatio) {
                    entry.pixel_aspect = Number(interpretation.pixelAspectRatio);
                }
            }
        } catch (e2) { /* stills and synthetics have none */ }

        return entry;
    }

    function readDuration(item) {
        try {
            var out = item.getOutPoint();
            var inPoint = item.getInPoint();
            return U.secondsOf(out) - U.secondsOf(inPoint);
        } catch (e) {
            return safe(function () { return U.secondsOf(item.duration); }, 0);
        }
    }

    function guessType(mediaPath, item, meta) {
        var lower = String(mediaPath).toLowerCase();
        if (/\.(png|jpg|jpeg|tif|tiff|psd|gif|bmp|webp|tga|exr)$/.test(lower)) {
            return 'still';
        }
        if (/\.(wav|mp3|aac|aif|aiff|m4a|flac|ogg)$/.test(lower)) {
            return 'audio';
        }
        if (/\.(mp4|mov|avi|mxf|mkv|m4v|mts|r3d|braw|webm|wmv)$/.test(lower)) {
            return 'video';
        }
        // No usable extension: ask the item whether it has video.
        var hasVideo = safe(function () {
            return item.hasVideo && item.hasVideo();
        }, null);
        if (hasVideo === true) { return 'video'; }
        if (hasVideo === false) { return 'audio'; }
        return 'unknown';
    }

    function assets(params) {
        if (!app.project) { throw U.fail('No project is open'); }
        var options = {
            recursive: U.defined(params.recursive, true),
            media_type: params.media_type || 'any'
        };
        var out = [];

        var root = app.project.rootItem;
        if (params.bin) {
            var bin = findBin(root, String(params.bin));
            if (!bin) {
                throw U.fail('No bin named "' + params.bin + '"', {
                    hint: 'Call project.assets without a bin to see everything'
                });
            }
            walk(bin, out, options, String(bin.name), 0);
        } else {
            walk(root, out, options, '', 0);
        }
        return { count: out.length, assets: out };
    }

    function findBin(parent, name) {
        for (var i = 0; i < parent.children.numItems; i++) {
            var child = parent.children[i];
            if (safe(function () { return child.type; }, 0) !== 2) { continue; }
            if (U.nameMatches(child.name, name)) { return child; }
            var nested = findBin(child, name);
            if (nested) { return nested; }
        }
        return null;
    }

    /**
     * Find a project item by name, path or basename.
     *
     * Deliberately forgiving in a specific order -- exact path, exact name,
     * basename, then substring -- so an unambiguous reference always wins and
     * a loose one only matches when nothing better does.
     */
    function findAsset(reference) {
        if (!reference) { throw U.fail('No asset given'); }
        var wanted = String(reference);
        var found = { path: null, name: null, base: null, loose: null };
        var seen = [];

        function visit(parent, depth) {
            if (depth > 12) { return; }
            for (var i = 0; i < parent.children.numItems; i++) {
                var child = parent.children[i];
                if (safe(function () { return child.type; }, 0) === 2) {
                    visit(child, depth + 1);
                    continue;
                }
                var name = String(child.name);
                seen[seen.length] = name;
                var media = safe(function () {
                    return String(child.getMediaPath());
                }, '');

                if (media && media === wanted) { found.path = child; }
                if (!found.name && U.nameMatches(name, wanted)) { found.name = child; }
                if (!found.base && media && basename(media) === basename(wanted)) {
                    found.base = child;
                }
                if (!found.loose &&
                    U.normalise(name).indexOf(U.normalise(wanted)) !== -1) {
                    found.loose = child;
                }
            }
        }
        visit(app.project.rootItem, 0);

        var match = found.path || found.name || found.base || found.loose;
        if (!match) {
            throw U.fail('No asset in the project matches "' + wanted + '"', {
                hint: 'Import it first with project.import, or call '
                    + 'project.assets to see what is available',
                detail: { available: seen.slice(0, 60) }
            });
        }
        return match;
    }

    function importFiles(params) {
        if (!app.project) { throw U.fail('No project is open'); }
        var paths = params.paths;
        if (!U.isArray(paths) || !paths.length) {
            throw U.fail('project.import needs a list of paths');
        }

        var missing = [];
        for (var i = 0; i < paths.length; i++) {
            if (!File(paths[i]).exists) { missing[missing.length] = paths[i]; }
        }
        if (missing.length) {
            throw U.fail('File(s) not found: ' + missing.join(', '), {
                hint: 'Paths must be absolute and on the machine running Premiere'
            });
        }

        var targetBin = app.project.rootItem;
        if (params.bin) {
            targetBin = findBin(app.project.rootItem, String(params.bin));
            if (!targetBin) {
                targetBin = app.project.rootItem.createBin(String(params.bin));
            }
        }

        var before = countItems(targetBin);
        var ok = app.project.importFiles(
            paths, true, targetBin, !!params.as_numbered_stills);
        if (ok === false) {
            throw U.fail('Premiere refused the import', {
                hint: 'Unsupported codec, or the file is in use by another app'
            });
        }
        var after = countItems(targetBin);

        // Report what actually landed, by name, so the next operation can
        // reference it without a second round trip.
        var imported = [];
        for (var c = before; c < after; c++) {
            imported[imported.length] = String(targetBin.children[c].name);
        }
        return {
            imported: imported.length || paths.length,
            names: imported,
            bin: String(targetBin.name)
        };
    }

    function countItems(bin) {
        try { return bin.children.numItems; } catch (e) { return 0; }
    }

    function createBin(params) {
        var parent = app.project.rootItem;
        if (params.parent) {
            var found = findBin(parent, String(params.parent));
            if (found) { parent = found; }
        }
        var existing = findBin(parent, String(params.name));
        if (existing) {
            return { bin: String(existing.name), created: false };
        }
        var bin = parent.createBin(String(params.name));
        return { bin: String(bin.name), created: true };
    }

    // ------------------------------------------------------------------
    // Sequences
    // ------------------------------------------------------------------

    function listSequences() {
        var out = [];
        for (var i = 0; i < app.project.sequences.numSequences; i++) {
            var sequence = app.project.sequences[i];
            var size = U.frameSize(sequence);
            out[out.length] = {
                name: String(sequence.name),
                id: String(sequence.sequenceID),
                fps: U.sequenceFps(sequence),
                width: size.width,
                height: size.height,
                duration: U.secondsOf(sequence.end),
                video_tracks: sequence.videoTracks.numTracks,
                audio_tracks: sequence.audioTracks.numTracks,
                active: !!(app.project.activeSequence &&
                           app.project.activeSequence.sequenceID === sequence.sequenceID)
            };
        }
        return { count: out.length, sequences: out };
    }

    function sequenceInfo() {
        var sequence = U.activeSequence();
        var size = U.frameSize(sequence);
        var out = {
            name: String(sequence.name),
            id: String(sequence.sequenceID),
            fps: U.sequenceFps(sequence),
            width: size.width,
            height: size.height,
            duration: U.secondsOf(sequence.end),
            video_tracks: sequence.videoTracks.numTracks,
            audio_tracks: sequence.audioTracks.numTracks
        };
        out.playhead = safe(function () {
            return U.secondsOf(sequence.getPlayerPosition());
        }, 0);
        out['in'] = safe(function () { return U.secondsOf(sequence.getInPoint()); }, 0);
        out.out = safe(function () { return U.secondsOf(sequence.getOutPoint()); }, 0);
        return out;
    }

    function activateSequence(params) {
        var wanted = params.name || params.id;
        for (var i = 0; i < app.project.sequences.numSequences; i++) {
            var sequence = app.project.sequences[i];
            if ((params.id && String(sequence.sequenceID) === String(params.id)) ||
                (params.name && U.nameMatches(sequence.name, params.name))) {
                app.project.activeSequence = sequence;
                return { active: String(sequence.name) };
            }
        }
        throw U.fail('No sequence named "' + wanted + '"', {
            hint: 'sequence.list shows what exists'
        });
    }

    function createSequence(params) {
        var name = String(params.name);
        if (params.from_asset) {
            var asset = findAsset(params.from_asset);
            try {
                app.project.createNewSequenceFromClips(name, [asset]);
                return { created: name, from: String(asset.name) };
            } catch (e) {
                throw U.fail('Could not create a sequence from that clip: ' + e);
            }
        }
        if (params.preset) {
            if (!File(params.preset).exists) {
                throw U.fail('Preset not found: ' + params.preset);
            }
            app.project.newSequence(name, String(params.preset));
            return { created: name, preset: String(params.preset) };
        }
        try {
            app.project.newSequence(name, '');
            return { created: name };
        } catch (e2) {
            throw U.fail('Creating an empty sequence needs a preset on this build', {
                hint: 'Pass "preset" (a .sqpreset path) or "from_asset"'
            });
        }
    }

    return {
        info: info,
        path: path,
        save: save,
        open: open,
        assets: assets,
        findAsset: findAsset,
        findBin: findBin,
        importFiles: importFiles,
        createBin: createBin,
        listSequences: listSequences,
        sequenceInfo: sequenceInfo,
        activateSequence: activateSequence,
        createSequence: createSequence
    };
}());
