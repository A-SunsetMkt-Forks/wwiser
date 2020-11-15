import logging
from . import wtxtp_util, wgamesync


# Takes the parsed bank nodes and rebuilds them to simpler objects with quick access
# for main useful (sound) attributes, and has helper functions to write TXTP

#******************************************************************************

class Rebuilder(object):
    def __init__(self):
        self.DEFAULT_CLASS = _CAkNone

        self._node_refs = {}        #sid > parser node
        self._sid_refs = {}         #parser node > sid
        self._bnodes = {}           #sid > rebuilt node

        self._media_banks = {}      #bank + sid > internal wem index
        self._media_sids = {}       #sid > bank + internal wem index
        self._missing_nodes = {}
        self._missing_media = {}
        self._transition_objects = 0
        self._unknown_props = {}

        # after regular generation we want a list of nodes that weren't used, and
        # generate TXTP for them, but ordered by types since generating some types
        # may end up using other unused types
        self._used_refs = {}        # marks which node_refs has been used
        self._name_refs = {}        # registered types > list of nodes
        # transitions (separate to avoid counting as unused)
        self._transition_nodes = {} # node


        self._generated_hircs =  [
            'CAkEvent',
            'CAkDialogueEvent',
        ]

        # only parsed hircs at the moment are those that contribute to .txtp
        self._hircs = {
            #base
            'CAkEvent': _CAkEvent,
            'CAkDialogueEvent': _CAkDialogueEvent,
            'CAkActionPlay': _CAkActionPlay,
            'CAkActionTrigger': _CAkActionTrigger,

            #not found, may need to do something with them
            'CAkActionPlayAndContinue': _CAkActionPlayAndContinue,
            'CAkActionPlayEvent': _CAkActionPlayEvent,

            #sound engine
            'CAkLayerCntr': _CAkLayerCntr,
            'CAkSwitchCntr': _CAkSwitchCntr,
            'CAkRanSeqCntr': _CAkRanSeqCntr,
            'CAkSound': _CAkSound,

            #music engine
            'CAkMusicSwitchCntr': _CAkMusicSwitchCntr,
            'CAkMusicRanSeqCntr': _CAkMusicRanSeqCntr,
            'CAkMusicSegment': _CAkMusicSegment,
            'CAkMusicTrack': _CAkMusicTrack,

            #others
            'CAkStinger': _CAkStinger,
            'CAkState': _CAkState,

            #not useful
            #CAkActorMixer
            #CAkActionSetState
            #CAkAction*
            #CAkBus
            #CAkAuxBus
            #CAkFeedbackBus
            #CAkFeedbackNode
            #CAkAttenuation
            #CAkAudioDevice
            #CAkFxShareSet
            #CAkFxCustom
            #CAkLFOModulator
            #CAkEnvelopeModulator
            #CAkTimeModulator
        }

        # ordered by priority (needed to generate unused)
        self._unused_hircs = [
            #'CAkEvent': _CAkEvent,
            #'CAkDialogueEvent': _CAkDialogueEvent,
            'CAkActionPlay',
            'CAkActionTrigger',

            'CAkActionPlayAndContinue',
            'CAkActionPlayEvent',

            'CAkLayerCntr',
            'CAkSwitchCntr',
            'CAkRanSeqCntr',
            'CAkSound',

            'CAkMusicSwitchCntr',
            'CAkMusicRanSeqCntr',
            'CAkMusicSegment',
            'CAkMusicTrack',
        ]

        return

    def get_missing_nodes(self):
        return len(self._missing_nodes)

    def get_missing_media(self):
        return len(self._missing_media)

    def get_transition_objects(self):
        return self._transition_objects

    def get_unknown_props(self):
        return self._unknown_props

    def get_generated_hircs(self):
        return self._generated_hircs

    #--------------------------------------------------------------------------

    def add_node_ref(self, sid, node):
        # objects can be repeated when saved to different banks, but should be
        # clones so no matter which is chosen (ex. Magatsu Wahrheit, Ori and the Will of the Wisps)

        self._sid_refs[id(node)] = sid #do save id > sid for minor speedup
        if self._node_refs.get(sid) is not None:
            #logging.info("generator: repeated id %i", sid)
            return
        self._node_refs[sid] = node

        name = node.get_name()
        if name not in self._name_refs:
            self._name_refs[name] = []
        self._name_refs[name].append(node)

    def get_node_ref(self, tid):
        return self._node_refs.get(tid)


    def add_transition_segment(self, tid):
        # transition nodes in switches don't get used, register manually to generate at the end
        if not tid:
            return

        node = self.get_node_ref(tid)
        if not node:
            return

        self._transition_nodes[id(node)] = node
        self._get_bnode(node, tid) #force parse/register, but don't use yet
        return

    def get_transition_segments(self):
        return self._transition_nodes.values()

    def empty_transition_segments(self):
        self._transition_nodes = {}


    def has_unused(self):
        # find if useful nodes where used
        for hirc_name in self._unused_hircs:
            list = self._name_refs.get(hirc_name, [])
            for node in list:
                if id(node) not in self._used_refs:
                    name = node.get_name()
                    #remove some false positives
                    if name == 'CAkMusicSegment':
                        #unused segments may not have child nodes (silent segments are ignored)
                        bnode = self._get_bnode(node, mark_used=False)
                        if bnode and bnode.ntids:
                            return True
        return False

    def get_unused_names(self):
        return self._unused_hircs

    def get_unused_list(self, hirc_name):
        results = []
        list = self._name_refs.get(hirc_name, [])
        for node in list:
            if id(node) not in self._used_refs:
                results.append(node)
        return results

    #--------------------------------------------------------------------------

    # A game could load bgm.bnk + media1.bnk, and bgm.bnk point to sid=123 in media1.bnk.
    # But if user loads bgm1.bnk + media1.bnk + media2.bnk both media banks may contain sid=123,
    # so media_banks is used to find the index inside a certain bank (sid repeats allowed) first,
    # while media_sids is used to find any bank+index that contains that sid (repeats ignored).
    def add_media_index(self, bankname, sid, index):
        self._media_banks[(bankname, sid)] = index
        if sid not in self._media_sids:
            self._media_sids[sid] = (bankname, index)

    def get_media_index(self, bankname, sid):
        #seen 0 in v112 test banks
        if not sid:
            return None

        # try in current bank
        index = self._media_banks.get((bankname, sid))
        if index is not None:
            return (bankname, index)

        # try any bank
        media = self._media_sids.get(sid)
        if media is not None:
            return media

        #logging.info("generator: missing memory wem %s", sid)
        self._missing_media[sid] = True
        return None

    def _get_bnode_tid(self, tid, sid):
        node = self.get_node_ref(tid)
        if node:
            bnode = self._get_bnode(node)
        else:
            bnode = None

        if not bnode:
            #logging.info("generator: node %s not found, called by %s", tid, sid)
            self._missing_nodes[tid] = True

        return bnode

    def _get_bnode(self, node, sid=None, mark_used=True):
        if not node:
            return None
        if not sid:

            sid = self._sid_refs.get(id(node))
            if not sid:
                #this shouldn't happen id add_node_ref did its thing
                nsid = node.find1(type='sid') #slower
                if not nsid:
                    sid = 0
                else:
                    sid = nsid.value()
                logging.info("generator: unregistered node sid %i (can be ignored)", sid)
        if not sid:
            return None

        bnode = self._bnodes.get(sid)
        if bnode:
            return bnode

        # pre-process nodes with a helper class and save to cache
        # (some banks get huge and call the same things again and again, it gets quite slow to parse every time)
        name = node.get_name()
        bclass = self._hircs.get(name, self.DEFAULT_CLASS)

        bnode = bclass()
        bnode.init_builder(self)
        bnode.init_node(node)

        self._bnodes[sid] = bnode
        if mark_used:
            self._used_refs[id(node)] = True #register usage for unused detection
        return bnode

    #--------------------------------------------------------------------------

    def begin_txtp(self, txtp, node):
        bnode = self._get_bnode(node)
        if not bnode:
            return

        root_config = wtxtp_util.NodeConfig()
        txtp.begin(node, root_config)
        bnode.make_txtp(txtp)
        return

    def begin_txtp_stinger(self, txtp, stinger):
        bnode = self._get_bnode(stinger.node, stinger.ntrigger.value())
        if not bnode:
            return

        # not correct since CAkStinger have no sid (same TriggerID can call different segments),
        # this is to show info
        bnode.sid = stinger.ntrigger.value()
        bnode.nsid = stinger.ntrigger
        bnode.ntid = stinger.ntid

        #self._process_next(ntid, txtp)
        root_config = wtxtp_util.NodeConfig()
        txtp.begin(stinger.node, root_config, nname=stinger.ntrigger, ntid=stinger.ntrigger, ntidsub=stinger.ntid)
        bnode.make_txtp(txtp)
        return

#******************************************************************************

# common for all 'rebuilt' nodes
class _NodeHelper(object):
    def __init__(self):
        pass #no params since changing constructors is a pain

    def init_builder(self, builder):
        self.builder = builder

    def init_node(self, node):
        #self.version = node.get_root().get_version()
        self.node = node
        self.name = node.get_name()
        self.nsid = node.find1(type='sid')
        self.sid = None
        if self.nsid:
            self.sid = self.nsid.value()

        self.config = wtxtp_util.NodeConfig()
        self.silences = None
        self.nfields = []   #main node fields, for printing (tuples for composite info)
        self.nattrs = []    #node attributes, in generic (key,value) form
        self.stingers = []

        self._build(node)

    #--------------------------------------------------------------------------

    def _barf(self, text="not implemented"):
        raise ValueError("%s - %s %s" % (text, self.name, self.sid))

    def _is_node(self, ntid):
        tid = ntid.value()
        node = self.builder.get_node_ref(tid)
        return node is not None

    def _process_next(self, ntid, txtp):
        tid = ntid.value()
        if tid == 0:
            #this is fairly common in switches, that may define all combos, but some nodes don't point to anything
            return

        bnode = self.builder._get_bnode_tid(tid, self.sid)
        if not bnode:
            return

        #logging.info("next: %s %s > %s", self.node.get_name(), self.sid, tid)
        bnode.make_txtp(txtp)
        return

    #--------------------------------------------------------------------------

    def _build(self, node):
        self._barf()
        return


    WARN_PROPS = [
        #"[TrimInTime]", "[TrimOutTime]", #seen in CAkState (ex. DMC5)
        #"[FadeInCurve]", "[FadeOutCurve]", #seen in CAkState, used in StateChunks (ex. NSR)
        "[LoopStart]", "[LoopEnd]",
        "[FadeInTime]", "[FadeOutTime]", "[LoopCrossfadeDuration]",
        "[CrossfadeUpCurve]", "[CrossfadeDownCurve]",
    ]
    OLD_AUDIO_PROPS = [
        'Volume', 'Volume.min', 'Volume.max', 'LFE', 'LFE.min', 'LFE.max', 
        'Pitch', 'Pitch.min', 'Pitch.max', 'LPF', 'LPF.min', 'LPF.max',
    ]
    OLD_ACTION_PROPS = [
        'tDelay', 'tDelayMin', 'tDelayMax', 'TTime', 'TTimeMin', 'TTimeMax',
    ]

    def _parse_props(self, ninit):
        nvalues = ninit.find(name='AkPropBundle<AkPropValue,unsigned char>')
        if not nvalues:
            nvalues = ninit.find(name='AkPropBundle<float,unsigned short>')
        if not nvalues:
            nvalues = ninit.find(name='AkPropBundle<float>')
        if nvalues: #newer
            nprops = nvalues.finds(name='AkPropBundle')
            for nprop in nprops:
                nkey = nprop.find(name='pID')
                nval = nprop.find(name='pValue')

                valuefmt = nkey.get_attr('valuefmt')
                value = nval.value()
                if any(prop in valuefmt for prop in self.WARN_PROPS):
                    #self._barf('found prop %s' % (valuefmt))
                    self.builder._unknown_props[valuefmt] = True

                if "[Loop]" in valuefmt:
                    self.config.loop = value

                if "[Volume]" in valuefmt:
                    self.config.volume = value

                if "[DelayTime]" in valuefmt:
                    self.config.delay = value

                if "[InitialDelay]" in valuefmt:
                    self.config.idelay = value * 1000.0 #float in seconds to ms

                #missing useful effects:
                #TransitionTime: used in play events to fade-in event
                #Pitch: changes pitch

                self.nfields.append((nkey, nval))

        #todo ranged values
        nranges = ninit.find(name='AkPropBundle<RANGED_MODIFIERS<AkPropValue>>')
        if nranges: #newer
            nprops = nranges.finds(name='AkPropBundle')
            for nprop in nprops:
                nkey = nprop.find(name='pID')
                nmin = nprop.find(name='min')
                nmax = nprop.find(name='max')

                self.nfields.append((nkey, nmin, nmax))

        return nvalues or nranges


    def _build_action_config(self, node):
        ninit = node.find1(name='ActionInitialValues')
        if not ninit:
            return

        ok = self._parse_props(ninit)
        if ok:
            return

        #todo
        #may use PlayActionParams + eFadeCurve when TransitionTime is used to make a fade-in (goes after delay)

        #older
        for prop in self.OLD_ACTION_PROPS:
            nprop = ninit.find(name=prop)
            if not nprop:
                continue
            value = nprop.value()

            #fade-in curve
            #if value != 0 and (prop == 'TTime' or prop == 'TTimeMin'):
            #    self._barf("found " + prop)

            if value != 0 and (prop == 'tDelay' or prop == 'tDelayMin'):
                self.config.idelay = value

            if value != 0: #default to 0 if not set
                self.nfields.append(nprop)


    def _build_audio_config(self, node):
        name = node.get_name()

        # find songs that silence files to crossfade
        # mainly useful on Segment/Track level b/c usually games that set silence on
        # Switch/RanSeq do nothing interesting with it (ex. just to silence the whole song)
        check_state = name in ['CAkMusicTrack', 'CAkMusicSegment']
        check_rtpc = check_state
        nbase = node.find1(name='NodeBaseParams')
        if nbase and check_state:
            # state sets silence value (ex. MGR double tracks)
            nstatechunk = nbase.find1(name='StateChunk')
            if nstatechunk:
                nstateids = nstatechunk.finds(name='ulStateInstanceID')
                for nstateid in nstateids:
                    tid = nstateid.value()
                    nstate = self.builder.get_node_ref(nstateid.value())
                    bstate = self.builder._get_bnode(nstate, tid)

                    self.silences = bstate and bstate.config.volume and bstate.config.volume <= -96.0
                    if self.silences:
                        #logging.info("generator: state silence found %s %s %s" % (self.sid, tid, node.get_name()))
                        nstategroupid = nstatechunk.find1(name='ulStateGroupID')
                        self.nfields.append(nstategroupid)

        if nbase and check_rtpc:
            # RTPC linked to volume (ex. DMC5 battle rank layers)
            nrtpc = nbase.find1(name='RTPC')
            if nrtpc:
                nparam = nrtpc.find1(name='ParamID')
                if nparam and nparam.value() == 0: #volume
                    #logging.info("generator: RTPC silence found %s %s" % (self.sid, node.get_name()))
                    self.silences = True
                    nrptcid = nrtpc.find1(name='RTPCID')
                    self.nfields.append(nrptcid)

        # find other parameters
        ninit = node.find1(name='NodeInitialParams')
        if not ninit:
            ninit = node.find1(name='StateInitialValues')
        if not ninit:
            return

        ok = self._parse_props(ninit)
        if ok:
            return

        #older
        for prop in self.OLD_AUDIO_PROPS:
            nprop = ninit.find(name=prop)
            if not nprop:
                continue
            value = nprop.value()
            if value != 0 and prop == 'Volume':
                self.config.volume = value #also min/max

            if value != 0: #default to 0 if not set
                self.nfields.append(nprop)

    def _build_transitions(self, node, is_switch):
        #AkMeterInfo: into for transitions
        #pRules > AkMusicTransitionRule: when gamesync changes from one node to other (with fades or transition nodes)
        #- srcID/dstID: source/destination node, id=object, -1=any, 0=nothing
        #- other values: how to sync (like change when reaching node's "ExitMarker" cue)
        #  (may only be used if transitionTime > 0?)
        # * AkMusicTransitionObject: print

        nmtos = node.finds(name='AkMusicTransitionObject')
        # older versions use bIsTransObjectEnabled to signal use, but segmentID is 0 if false anyway
        for nmto in nmtos:
            ntid = nmto.find1(name='segmentID')
            if ntid and ntid.value() != 0:
                if is_switch:
                    self.builder.add_transition_segment(ntid.value())
                else:
                    # rare in playlists (Polyball, Spiderman)
                    self.builder._transition_objects += 1

        return

    def _build_stingers(self, node):
        nstingers = node.finds(name='CAkStinger')
        if not nstingers:
            return

        for nstinger in nstingers:
            stinger = wtxtp_util.NodeStinger()
            stinger.node = nstinger
            stinger.ntrigger = nstinger.find1(name='TriggerID') #idExt called from trigger action
            stinger.ntid = nstinger.find1(name='SegmentID') #segment to play (may be 0)
            if stinger.ntid and stinger.ntid.value() != 0:
                self.stingers.append(stinger)
        return

    def _build_silence(self, node, clip):
        sound = wtxtp_util.NodeSound()
        sound.nsrc = node
        sound.silent = True
        sound.clip = clip
        return sound

    def _parse_source(self, node):
        source = wtxtp_util.NodeSource(node)
        return source

    #--------------------------------------------------------------------------

    #tree with multi gamesync (order of gamesyncs is branch order in tree)
    def _build_tree(self, node, ntree):
        self.args = []
        self.paths = []
        self.npaths = []

        # tree starts from AkDecisionTree and a base 'pNodes' (list) + 1 'Node' (object), then it has:
        #   pNodes + Node xN (key + audioNodeId) = 1 gamesync
        #   pNodes + None xN (key + N children) > (xN) > pNodes Node xN (key + audioNodeId) = N gamesyncs
        # (so N branches per N gamesyncs)

        # args has gamesync type+names, and tree "key" is value (where 0=any)
        depth = node.find1(name='uTreeDepth').value()
        nargs = node.finds(name='AkGameSync')
        if depth != len(nargs):
            self._barf(text="tree depth and args don't match")

        self.args = []
        for narg in nargs:
            ngtype = narg.find(name='eGroupType')
            ngname = narg.find(name='ulGroup')
            if ngtype:
                gtype = ngtype.value()
            else: #assumed default for DialogueEvent in older versions
                gtype = wgamesync.GamesyncParams.TYPE_STATE
            self.args.append( (gtype, ngname) )

        #simplify tree to a list of gamesyncs pointing to an id, basically how the editor shows them
        # [(gtype1, ngname1, ngvalue1), (gtype2, ngname2, ngvalue2), ...] > ntid
        gamesyncs = [None] * len(nargs) #temp list

        nnodes = ntree.find1(name='pNodes').find1(name='Node').find1(name='pNodes') #base
        if nnodes: #may be empty
            self._build_tree_nodes(node, self.args, 0, nnodes, gamesyncs)


    def _build_tree_nodes(self, node, args, depth, nnodes, gamesyncs):
        if depth >= len(args):
            return None #shouldn't get here
        gtype, ngname = args[depth]

        for nnode in nnodes.get_children():
            ngvalue = nnode.find1(name='key')
            nsubnodes = nnode.find1(name='pNodes')

            gamesyncs[depth] = (gtype, ngname, ngvalue)
            if depth + 1 == len(args):
                ntid = nnode.find1(name='audioNodeId')
                self._build_tree_path(gamesyncs, ntid)
            else:
                self._build_tree_nodes(node, args, depth + 1, nsubnodes, gamesyncs)
        return

    def _build_tree_path(self, gamesyncs, ntid):
        #clone list of gamesyncs and final ntid (both lists as an optimization for huge trees)
        npath = []
        path = []
        for gtype, ngname, ngvalue in gamesyncs:
            npath.append( (gtype, ngname, ngvalue) )
            path.append( (gtype, ngname.value(), ngvalue.value()) )
        self.paths.append( (path, ntid) )
        self.npaths.append( (npath, ntid) )

        return

    def _tree_get_npath(self, txtp, npaths):
        # find gamesyncs matches in path (matches exact values)

        #ignore switches with empty tree
        if not npaths:
            return None

        tmp_args = {}
        for gtype, ngname in self.args:
            gvalue = txtp.params.value(gtype, ngname.value())
            tmp_args[(gtype, ngname.value())] = gvalue
            #logging.info("generator: arg %s %s %s", gtype, ngname.value(), gvalue)

        for npath, ntid in npaths:
            if self._tree_path_ok(txtp, tmp_args, npath):
                #logging.info("generator: found ntid %s", ntid.value())
                return (npath, ntid)

        #logging.warn("generator: path not found in %s", self.sid)
        return None

    def _tree_path_ok(self, txtp, args, npath):
        for gtype, ngname, ngvalue in npath:
            #get combo-GS and compare to path-GS
            gvalue = args.get((gtype, ngname.value()))
            if gvalue is None:
                #combo-GS value not set for path-GS
                return False
            if gvalue != ngvalue.value():
                return False
        return True

    #--------------------------------------------------------------------------

    def make_txtp(self, txtp):
        try:
            txtp.info_next(self.node, self.nfields, nattrs=self.nattrs, nsid=self.nsid)
            self._process_txtp(txtp)
            txtp.info_done()
        except Exception: #as e #autochained
            raise ValueError("Error processing TXTP for node %i" % (self.sid)) #from e

    def _process_txtp(self, txtp):
        self._barf("must implement")

#******************************************************************************

#non-audio node, doesn't contribute to txtp
class _CAkNone(_NodeHelper):
    def __init__(self):
        super(_CAkNone, self).__init__()

    def _build(self, node):
        #ignore
        return

    def make_txtp(self, txtp):
        #don't print node info in txtp
        return

    #def _process_txtp(self, txtp):
    #    return

# todo improve (stingers have no sid so it's set externally)
class _CAkStinger(_NodeHelper):
    def __init__(self):
        super(_CAkStinger, self).__init__()
        self.ntid = None #external

    def _build(self, node):
        #ignore
        return

    def _process_txtp(self, txtp):
        self._process_next(self.ntid, txtp)
        return

#non-audio node, but it's used as a reference
class _CAkState(_NodeHelper):
    def __init__(self):
        super(_CAkState, self).__init__()

    def _build(self, node):
        self._build_audio_config(node)
        #save config (used to check silences)
        return

    def make_txtp(self, txtp):
        #don't print node info in txtp
        return

#******************************************************************************

class _CAkEvent(_NodeHelper):
    def __init__(self):
        super(_CAkEvent, self).__init__()
        self.ntids = None

    def _build(self, node):
        self.ntids = node.finds(name='ulActionID')
        return

    def _process_txtp(self, txtp):
        # N play actions are layered (may set a delay)
        txtp.group_layer(self.ntids, self.config)
        for ntid in self.ntids:
            self._process_next(ntid, txtp)
        txtp.group_done(self.ntids)
        return

#******************************************************************************

class _CAkDialogueEvent(_NodeHelper):
    def __init__(self):
        super(_CAkDialogueEvent, self).__init__()
        #self.paths = []
        #self.npaths = []

    def _build(self, node):
        self._build_audio_config(node)
        if self.config.loop is not None:
            self._barf("loop flag")

        ntree = node.find(name='AkDecisionTree')
        if ntree:
            self._build_tree(node, ntree)

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        # set all gamesyncs
        if txtp.params.empty:
            for path, ntid in self.paths:
                txtp.ppaths.adds(path)
                self._process_next(ntid, txtp)
                txtp.ppaths.done()
            return

        # find if current gamesync combo matches one of the paths
        npath_combo = self._tree_get_npath(txtp, self.npaths) #then with default path
        if npath_combo:
            npath, ntid = npath_combo
            txtp.info_gamesyncs(npath)
            self._process_next(ntid, txtp)
        return

#******************************************************************************

class _CAkAction(_NodeHelper):
    def __init__(self):
        super(_CAkAction, self).__init__()
        self.ntid = None

    def _build(self, node):
        self._build_action_config(node)

        ntid = node.find(name='idExt')
        if not ntid: #older
            ntid = node.find(name='ulTargetID')
            #tDelay
            #tDelayMin
            #tDelayMax

        #todo PlayActionParams
        self.ntid = ntid

        self._build_subaction(node)

    def _build_subaction(self, node):
        return

#******************************************************************************

class _CAkActionPlayAndContinue(_CAkAction):
    def __init__(self):
        super(_CAkActionPlayAndContinue, self).__init__()

    def _build(self, node):
        self._barf()


#******************************************************************************

class _CAkActionTrigger(_CAkAction):
    def __init__(self):
        super(_CAkActionTrigger, self).__init__()

    def _process_txtp(self, txtp):
        # Trigger calls current music object (mranseq/mswitch usually) defined CAkStinger,
        # which in turn links to some segment and stops.
        # Trigger events may come before CAkStingers, and one trigger may call
        # stingers from any song (1 trigger > N stingers), so they are handled
        # separatedly during mranseq/mswitch.

        #logging.info("generator: trigger %i not implemented", self.sid)
        return

#******************************************************************************

class _CAkActionPlay(_CAkAction):
    def __init__(self):
        super(_CAkActionPlay, self).__init__()

    def _process_txtp(self, txtp):
        # rare but may contain config
        txtp.group_single(self.config)
        self._process_next(self.ntid, txtp)
        txtp.group_done()
        return

#******************************************************************************

class _CAkActionPlayEvent(_CAkActionPlay): #_CAkActionPlay
    def __init__(self):
        super(_CAkActionPlayEvent, self).__init__()

#******************************************************************************

class _CAkSwitchCntr(_NodeHelper):
    def __init__(self):
        super(_CAkSwitchCntr, self).__init__()
        self.gtype = None
        self.ngname = None
        self.gvalue_ntids = {}

    def _build(self, node):
        self._build_audio_config(node)
        if self.config.loop is not None:
            self._barf("loop flag")

        self.gtype = node.find(name='eGroupType').value()
        self.ngname = node.find(name='ulGroupID')
        #ulDefaultSwitch: not used since we create all combos
        #bIsContinuousValidation: step/continuous mode?
        #ulNumSwitchParams: config for switches (ex. FadeOutTime/FadeInTime)
        #children: same as NodeList

        ngvalues = node.find(name='SwitchList').finds(name='ulSwitchID')
        for ngvalue in ngvalues:
            ntids = ngvalue.get_parent().find(name='NodeList').finds(type='tid')
            if not ntids: #may define an empty path
                continue
            gvalue = ngvalue.value()
            self.gvalue_ntids[gvalue] = (ntids, ngvalue)
        return

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        gtype = self.gtype
        gname = self.ngname.value()

        if txtp.params.empty:
            #set all gamesyncs
            for ntids, ngvalue in self.gvalue_ntids.values(): #order doesn't matter
                gvalue = ngvalue.value()
                txtp.ppaths.add(gtype, gname, ngvalue.value())
                for ntid in ntids:
                    self._process_next(ntid, txtp)
                txtp.ppaths.done()
            return

        #get current gamesync
        gvalue = txtp.params.value(gtype, gname)
        if gvalue is None:
            return
        if not gvalue in self.gvalue_ntids:
            return
        ntids, ngvalue = self.gvalue_ntids[gvalue]


        txtp.info_gamesync(gtype, self.ngname, ngvalue)
        txtp.group_layer(ntids, self.config)
        for ntid in ntids: #multi IDs are possible but rare (KOF13)
            self._process_next(ntid, txtp)
        txtp.group_done()
        return

#******************************************************************************

class _CAkRanSeqCntr(_NodeHelper):
    def __init__(self):
        super(_CAkRanSeqCntr, self).__init__()
        self.ntids = []

    def _build(self, node):
        self._build_audio_config(node)
        if self.config.loop is not None:
            self._barf("loop flag")

        #bIsGlobal: this object is a global entity (not needed, affects sequences/shuffles/etc)
        nmode = node.find(name='eMode')  #0=random / 1=sequence
        nrandom = node.find(name='eRandomMode')  #0=normal (repeatable), 1=shuffle (no repeatable)
        nloop = node.find(name='sLoopCount')  #1=once, 0=infinite, >1=N times
        ncontinuous = node.find(name='bIsContinuous')  #play one of the objects each time this is played, else play all

        self.mode = nmode.value()
        self.random = nrandom.value()
        self.config.loop = nloop.value()
        self.continuous = ncontinuous.value()

        #sLoopModMin/sLoopModMax: random loop modifiers (loop -min +max)

        #eTransitionMode: defines a transition type between objects (ex. "delay" + fTransitionTime)
        #fTransitionTime / fTransitionTimeModMin / fTransitionTimeModMax: values for transition (depending on mode)
        #ntmode = node.find(name='eTransitionMode')
        #if ntmode and ntmode.value() != 0:
        #    self._barf("ranseq transition")


        if   self.mode == 0: #random
            #wAvoidRepeatCount: N objects must be played before one is repeated (also depends on normal/shuffle)
            #_bIsUsingWeight: unused? (AkPlaylistItem always has weight)
            nitems = node.finds(name='AkPlaylistItem') #there is also children, but this has proper order
            for nitem in nitems:
                self.ntids.append( nitem.find(type='tid') )
                #self.nweights.append( nitem.find(name='weight') )

        elif self.mode == 1: #sequence
            #bResetPlayListAtEachPlay: resets from 1st object each time is event replayed (in continuous mode)
            #bIsRestartBackward: once done, play item from last to first
            self.ntids = node.find(name='Children').finds(type='tid') #not actually ordered?

        self.nfields.extend([nmode, nrandom, nloop, ncontinuous])
        return

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        if   self.mode == 0: #random
            txtp.group_random(self.ntids, self.config)
            for ntid in self.ntids:
                self._process_next(ntid, txtp)
            txtp.group_done(self.ntids)

        elif self.mode == 1: #sequence
            txtp.group_sequence(self.ntids, self.config)
            for ntid in self.ntids:
                self._process_next(ntid, txtp)
            txtp.group_done(self.ntids)

        return

#******************************************************************************

class _CAkLayerCntr(_NodeHelper):
    def __init__(self):
        super(_CAkLayerCntr, self).__init__()
        self.ntids = []

    def _build(self, node):
        self._build_audio_config(node)
        if self.config.loop is not None:
            self._barf("loop flag")

        nmode = node.find(name='bIsContinuousValidation')

        #if nmode: #newer only
        #if   mode == 0: #step (plays all at the same time, may loop or stop once all done)
        #elif mode == 1: #continuous (keeps playing nodes in RTPC region)

        self.ntids = node.find(name='Children').finds(type='tid')

        # usually found with RTPCs (ex. RPMs) + pLayers that define when layers are played
        #nlayers = node.finds(name='ulLayerID')
        #if nlayers:
        #    self._barf("multilayer found")

        if nmode:
            self.nfields.append(nmode)
        return

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        txtp.group_layer(self.ntids, self.config)
        for ntid in self.ntids:
            self._process_next(ntid, txtp)
        txtp.group_done(self.ntids)
        return

#******************************************************************************

class _CAkSound(_NodeHelper):
    def __init__(self):
        super(_CAkSound, self).__init__()
        self.sound = wtxtp_util.NodeSound()

    def _build(self, node):
        self._build_audio_config(node)

        nloop = node.find(name='Loop')
        if nloop: #older
            self.config.loop = nloop.value()
            self.nfields.append(nloop)
            #there is min/max too

        nitem = node.find(name='AkBankSourceData')
        source = self._parse_source(nitem)
        self.sound.source = source
        self.sound.nsrc = source.nfileid
        #if self._is_node(ntid): #source is an object (like FX)
        #    pass

        self.nfields.append(source.nstreamtype)
        if source.nsourceid != source.nfileid:
            self.nfields.append(source.nfileid)
        return

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        txtp.info_source(self.sound.nsrc, self.sound.source)
        txtp.source_sound(self.sound, self.config)
        return

#******************************************************************************

class _CAkMusicSwitchCntr(_NodeHelper):
    def __init__(self):
        super(_CAkMusicSwitchCntr, self).__init__()
        self.gtype = None
        self.ngname = None
        self.gvalue_ntid = {}
        self.has_tree = None
        #tree config
        #self.paths = []
        #self.npaths = []

    def _build(self, node):
        self._build_audio_config(node)
        self._build_transitions(node, True)
        self._build_stingers(node)

        #Children: list, also in nodes
        #bIsContinuePlayback: ?
        #uMode: 0=BestMatch/1=Weighted

        ntree = node.find(name='AkDecisionTree')
        if ntree:
            #later versions use a tree
            self.has_tree = True

            self._build_tree(node, ntree)

        else:
            #earlier versions work like a normal switch
            self.has_tree = False

            self.gtype = node.find(name='eGroupType').value()
            self.ngname = node.find(name='ulGroupID')
            #ulDefaultSwitch: not needed since we create all combos

            nswitches = node.find(name='pAssocs')
            ngvalues = nswitches.finds(name='switchID')
            for ngvalue in ngvalues:
                ntid = ngvalue.get_parent().find(name='nodeID')
                #if not ntid: #may define empty path?
                #    continue
                gvalue = ngvalue.value()
                self.gvalue_ntid[gvalue] = (ntid, ngvalue)

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        if self.has_tree:
            # set all gamesyncs
            if txtp.params.empty:
                txtp.ppaths.add_stingers(self.stingers)

                for path, ntid in self.paths:
                    txtp.ppaths.adds(path)
                    self._process_next(ntid, txtp)
                    txtp.ppaths.done()
                return

            # find if current gamesync combo matches one of the paths
            npath_combo = self._tree_get_npath(txtp, self.npaths)
            if npath_combo:
                npath, ntid = npath_combo
                txtp.info_gamesyncs(npath)
                self._process_next(ntid, txtp)
            return

        else:
            gtype = self.gtype
            gname = self.ngname.value()

            if txtp.params.empty:
                #set all gamesyncs
                for ntid, ngvalue in self.gvalue_ntid.values(): #order doesn't matter
                    gvalue = ngvalue.value()
                    txtp.ppaths.add(gtype, gname, ngvalue.value())
                    self._process_next(ntid, txtp)
                    txtp.ppaths.done()
                return

            # get current gamesync
            gvalue = txtp.params.value(gtype, gname)
            if gvalue is None:
                return
            if not gvalue in self.gvalue_ntid:
                return
            ntid, ngvalue = self.gvalue_ntid[gvalue]

            txtp.info_gamesync(gtype, self.ngname, ngvalue)
            self._process_next(ntid, txtp)
            return

        return

#******************************************************************************

class _CAkMusicRanSeqCntr(_NodeHelper):
    def __init__(self):
        super(_CAkMusicRanSeqCntr, self).__init__()
        self.items = []

    def _build(self, node):
        self._build_audio_config(node)
        if self.config.loop is not None:
            self._barf("loop flag")

        self._build_transitions(node, False)
        self._build_stingers(node)

        #playlists are "groups" that include 'leaf' objects or other groups
        # ex. item: playlist (sequence)
        #       item: segment A
        #       item: segment B
        #       item: playlist (random)
        #         item: segment C
        #         item: segment D
        # may play on loop: ABC ABC ABD ABC ABD ... (each group has its own loop config)
        nplaylist = node.find1(name='pPlayList')
        self._playlist(node, nplaylist, self.items)

    def _playlist(self, node, nplaylist, items):
        nitems = nplaylist.get_children()
        if not nitems:
            return

        for nitem in nitems:
            ntype = nitem.find1(name='eRSType')
            if not ntype: #older don't
                nchildren = nitem.find1(name='NumChildren')
                if nchildren and nchildren.value() == 0:
                    type = -1 #node
                else:
                    self._barf("unknown playlist type (old version?)")
            else:
                type = ntype.value()

            nloop = nitem.find1(name='Loop')

            #wAvoidRepeatCount
            #bIsUsingWeight
            #bIsShuffle
            nsubplaylist = nitem.find1(name='pPlayList')

            ntid = None
            if type == -1 or not nsubplaylist or nsubplaylist and not nsubplaylist.get_children():
                ntid = nitem.find(name='SegmentID') #0 on non-leaf nodes

            item = _CAkMusicRanSeqCntr_Item()
            item.nitem = nitem
            item.nfields = [ntype, nloop]
            item.ntid = ntid
            item.type = type
            item.config.loop = nloop.value()
            items.append(item)

            self._playlist(node, nsubplaylist, item.items)
        return

    def _process_txtp(self, txtp):
        if txtp.params.empty:
            txtp.ppaths.add_stingers(self.stingers)

        if self.silences:
            txtp.set_silences()

        txtp.group_single(self.config) #typically useless but may have volumes
        self._process_playlist(txtp, self.items)
        txtp.group_done()

    def _process_playlist(self, txtp, items):
        if not items:
            return

        for item in items:
            type = item.type
            subitems = item.items

            txtp.info_next(item.nitem, item.nfields)
            #leaf node uses -1 in newer versions, sid in older (ex. Enslaved)
            if type == -1 or item.ntid:
                transition = wtxtp_util.NodeTransition()
                transition.play_before = False

                txtp.group_single(item.config, transition=transition)
                self._process_next(item.ntid, txtp)
                txtp.group_done()
            else:
                if   type == 0: #0: ContinuousSequence (plays all objects, one after other)
                    txtp.group_sequence(subitems, item.config)
                elif type == 1: #1: StepSequence (picks one object, on next play/loop picks another)
                    txtp.group_random(subitems, item.config) #simulate
                elif type == 2: #2: ContinuousRandom (plays randoms until all objects are covered)
                    txtp.group_sequence(subitems, item.config) #simulate
                elif type == 3: #3: StepRandom (plays one random)
                    txtp.group_random(subitems, item.config)
                else:
                    self._barf('unknown type')
                self._process_playlist(txtp, item.items)
                txtp.group_done(subitems)
            txtp.info_done()

        return


class _CAkMusicRanSeqCntr_Item():
    def __init__(self):
        self.nitem = None
        self.nfields = []
        self.ntid = None
        self.type = None
        self.config = wtxtp_util.NodeConfig()
        self.items = []


#******************************************************************************

class _CAkMusicSegment(_NodeHelper):
    def __init__(self):
        super(_CAkMusicSegment, self).__init__()
        self.ntids = []
        self.sound = None
        self.sconfig = None

    def _build(self, node):
        self._build_audio_config(node)
        if self.config.loop is not None:
            self._barf("loop flag")

        #AkMeterInfo: for switches
        nfdur = node.find(name='fDuration')
        self.config.duration = nfdur.value()
        self.nfields.append(nfdur)

        nmarkers = node.find(name='pArrayMarkers')
        if nmarkers:
            #we want "entry" and "exit" markers (fixed IDs), but are ordered in time (may go in any position)
            nmid1 = nmarkers.find1(value=43573010)
            nmid2 = nmarkers.find1(value=1539036744)
            if not nmid1 or not nmid2:
                # older versions (v62<=) use IDs 0/1 for entry/exit (other cues do use tids)
                nmid1 = nmarkers.find1(value=0)
                nmid2 = nmarkers.find1(value=1)

            if not nmid1 or not nmid2:
                self._barf("entry/exit markers not found")

            nmarker1 = nmid1.get_parent()
            nmarker2 = nmid2.get_parent()

            nmpos1 = nmarker1.find(name='fPosition')
            nmpos2 = nmarker2.find(name='fPosition')

            self.config.entry = nmpos1.value()
            self.config.exit = nmpos2.value()

            self.nfields.append((nmarker1, nmpos1))
            self.nfields.append((nmarker2, nmpos2))
            #self.nfields.append(nm2.get_parent())
        else:
            self._barf('markers not found')


        self.ntids = node.find(name='Children').finds(type='tid')
        # empty segments are allowed as silence
        if not self.ntids:
            self.sound = self._build_silence(self.node, True)
            self.sconfig = wtxtp_util.NodeConfig()
        return

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        # empty segments are allowed as silence
        if not self.ntids:
            #logging.info("generator: found empty segment %s" % (self.sid))
            elems = [self.sound]
            txtp.group_layer(elems, self.config)
            txtp.source_sound(self.sound, self.sconfig)
            txtp.group_done(elems)
            return

        txtp.group_layer(self.ntids, self.config)
        for ntid in self.ntids:
            self._process_next(ntid, txtp)
        txtp.group_done(self.ntids)
        return


#******************************************************************************

class _CAkMusicTrack(_NodeHelper):
    def __init__(self):
        super(_CAkMusicTrack, self).__init__()
        self.type = None
        self.subtracks = []
        self.gtype = None
        self.ngname = None
        self.gvalue_index = {}

    def _build(self, node):
        self._build_audio_config(node)

        #todo clips probably can be plugins

        nloop = node.find(name='Loop')
        if nloop: #older
            self.config.loop = nloop.value()
            self.nfields.append(nloop)
            #there is min/max too

        # loops in MusicTracks are meaningless, ignore to avoid confusing the parser
        self.config.loop = None


        ntype = node.find(name='eTrackType')
        if not ntype:
            ntype = node.find(name='eRSType')
        self.type = ntype.value()

        #save info about sources for later
        streaminfos = {}
        nitems = node.find1(name='pSource').finds(name='AkBankSourceData')
        for nitem in nitems:
            source = self._parse_source(nitem)
            tid = source.nsourceid.value()
            streaminfos[tid] = source

        #each track contains "clips" (srcs):
        #- 0: silent track (ex. Astral Chain 517843579) 
        #- 1: normal 
        #- N: layered with fades if overlapped (pre-defined)
        #Final length size depends on segment
        ncount = node.find1(name='numSubTrack')
        if not ncount: #empty / no clips
            return

        self.subtracks = [None] * ncount.value()

        #map clips to subtracks
        nsrcs = node.finds(name='AkTrackSrcInfo')
        for nsrc in nsrcs:
            index = nsrc.find(name='trackID').value()
            if not self.subtracks[index]:
                self.subtracks[index] = []

            clip = self._build_clip(streaminfos, nsrc)
            self.subtracks[index].append(clip)

        #pre-parse switch variables
        if self.type == 3:
            #TransParams: define switch transition
            nswitches = node.find(name='SwitchParams')
            self.gtype = nswitches.find(name='eGroupType').value()
            self.ngname = nswitches.find(name='uGroupID')
            self.gvalue_index = {}

            ngvalues = nswitches.finds(name='ulSwitchAssoc')
            for ngvalue in ngvalues: #switch N = track N
                gvalue = ngvalue.value()
                index = ngvalue.get_parent().get_index()
                self.gvalue_index[gvalue] = (index, ngvalue)

        self.nfields.extend([ntype, ncount, ])
        return

    def _build_clip(self, streaminfos, nsrc):
        nfpa = nsrc.find(name='fPlayAt')
        nfbt = nsrc.find(name='fBeginTrimOffset')
        nfet = nsrc.find(name='fEndTrimOffset')
        nfsd = nsrc.find(name='fSrcDuration')
        nsourceid = nsrc.find(name='sourceID')
        neventid = nsrc.find(name='eventID') #later versions

        clip = _CAkMusicTrack_Clip()
        clip.nitem = nsrc
        clip.nfields = [nsourceid, neventid, nfpa, nfbt, nfet, nfsd]
        clip.neid = neventid

        clip.sound.fpa = nfpa.value()
        clip.sound.fbt = nfbt.value()
        clip.sound.fet = nfet.value()
        clip.sound.fsd = nfsd.value()

        sourceid = nsourceid.value()
        if sourceid: #otherwise has eventid
            source = streaminfos[sourceid]

            clip.sound.source = source
            clip.sound.nsrc = source.nsourceid

            clip.nfields.append(source.nstreamtype)
            if source.nsourceid != source.nfileid:
                clip.nfields.append(source.nfileid)
        return clip

    def _process_txtp(self, txtp):
        if self.silences:
            txtp.set_silences()

        if not self.subtracks: #empty / no clips
            return

        if   self.type == 0: #normal (plays one subtrack, N aren't allowed)
            if len(self.subtracks) > 1:
                raise ValueError("more than 1 track")
            txtp.group_single(self.config)
            for subtrack in self.subtracks:
                self._process_clips(subtrack, txtp)
            txtp.group_done()

        elif self.type == 1: #random (play one subtrack)
            txtp.group_random(self.subtracks, self.config)
            for subtrack in self.subtracks:
                self._process_clips(subtrack, txtp)
            txtp.group_done(self.subtracks)

        elif self.type == 2: #sequence (plays one subtrack, next time play another)
            txtp.group_random(self.subtracks, self.config)
            for subtrack in self.subtracks:
                self._process_clips(subtrack, txtp)
            txtp.group_done(self.subtracks)

        elif self.type == 3: #switch (plays one subtrack depending on variables)
            gtype = self.gtype
            gname = self.ngname.value()

            if txtp.params.empty:
                #set all gamesyncs
                for index, ngvalue in self.gvalue_index.values(): #order doesn't matter
                    gvalue = ngvalue.value()
                    txtp.ppaths.add(gtype, gname, ngvalue.value())
                    #no subnodes
                    txtp.ppaths.done()
                return

            #get current gamesync
            gvalue = txtp.params.value(gtype, gname)
            if gvalue is None:
                return
            if not gvalue in self.gvalue_index:
                return
            index, ngvalue = self.gvalue_index[gvalue]

            #play subtrack based on index (assumed to follow order as defined)
            txtp.info_gamesync(gtype, self.ngname, ngvalue)

            txtp.group_single(self.config)
            self._process_clips(self.subtracks[index], txtp)
            txtp.group_done()

        else:
            self._barf()

        return

    def _process_clips(self, subtrack, txtp):
        if not subtrack:
            #logging.info("generator: found empty subtrack %s" % (self.sid))
            #todo improve
            sound = self._build_silence(self.node, True)
            config = wtxtp_util.NodeConfig()
            sconfig = wtxtp_util.NodeConfig()
            elems = [sound]
            txtp.group_layer(elems, config)
            txtp.source_sound(sound, sconfig)
            txtp.group_done(elems)
            return

        config = wtxtp_util.NodeConfig()
        txtp.group_layer(subtrack, config)
        for clip in subtrack:
            if clip.neid and clip.neid.value():
                econfig = wtxtp_util.NodeConfig()
                econfig.idelay = clip.sound.fpa #uses FPA to start segment, should work ok
                txtp.group_single(econfig)
                self._process_next(clip.neid, txtp)
                txtp.group_done()
            else:
                sconfig = wtxtp_util.NodeConfig()
                sound = clip.sound
                txtp.info_next(clip.nitem, clip.nfields)
                txtp.info_source(clip.sound.nsrc, clip.sound.source)
                txtp.info_done()
                txtp.source_sound(clip.sound, sconfig)
        txtp.group_done(subtrack)
        return

class _CAkMusicTrack_Clip(_NodeHelper):
    def __init__(self):
        self.nitem = None
        self.nfields = []
        self.ntid = None
        self.neid = None
        self.sound = wtxtp_util.NodeSound()
        self.sound.clip = True
