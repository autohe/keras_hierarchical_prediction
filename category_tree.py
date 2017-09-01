import multiprocessing
from keras.activations import softmax
import keras.backend as K
from merge_loss_generator import MergeLossGenerator
import numpy as np

# original algorithm/code is from this URL
# https://github.com/pjreddie/darknet/blob/1e729804f61c8627eb257fba8b83f74e04945db7/src/tree.c
class CategoryTree:
    def __init__(self, tree, leaf=None, n_jobs=multiprocessing.cpu_count()):
        self.n_jobs=n_jobs
        
        # constract parent/child links and group segments
        self.labels, self.parents, self.is_leaf = self.serialize(tree,leaf=leaf)
        self.nlabels = len(self.labels)
        # alias
        self.nclasses = self.nlabels
        self.ncats = self.nlabels
        
        self.group_segments, group_nums, _ = self.rlencode(self.parents)
        # append a large index to access by zip(group_segments[:-1],group_segments[1:])
        self.group_segments = np.append(self.group_segments, [self.nlabels])

        self.child_group = [None] * self.nlabels
        for idx in range(self.nlabels)[::-1]:
            p_idx = self.parents[idx]
            if p_idx <0:
                continue
            gidx = np.where([idx in range(*r) for r in self.group_iter()])[0][0]
            self.child_group[p_idx]=gidx
            
        self.encoder = {l:idx for idx, l in enumerate(self.labels)}
    
    @staticmethod
    def set_label(cat, lut4conv, parents):
        n_hot_vector = np.zeros((len(parents),))
        if lut4conv is not None:
            cat = lut4conv[cat]
        n_hot_vector[cat] = 1
        parent = parents[cat]
        while parent >=0:
            n_hot_vector[parent] = 1
            parent = parents[parent]
        return n_hot_vector
            
    @staticmethod
    def set_label_wrap(args):
        return CategoryTree.set_label(*args)

    
    def to_hierarchical_categorical(self, ys, lut4conv=None):
        with multiprocessing.Pool(self.n_jobs) as p:
            n_hot_vectors = p.map(CategoryTree.set_label_wrap, [(y[0], lut4conv, self.parents) for y in ys])
        return np.array(n_hot_vectors)
                
        
    def generate_loss_func(self,mlg=None):
        def _loss_func(y_true,y_pred):
            return K.sum(y_true,axis=-1,keepdims=False) * K.categorical_crossentropy(y_true,y_pred)
        
        if mlg is None:
            self.mlg = MergeLossGenerator()
        else:
            self.mlg = mlg
        for (s,e) in self.group_iter():
            self.mlg.register(_loss_func,(s,e))
        return self.mlg.loss
    def hierarchical_softmax(self, x, axis=-1):
        bufs = [softmax(x[:,s:e],axis) for (s,e) in self.group_iter()]
        return K.concatenate(bufs, axis=-1)
    
    def group_iter(self):
        return zip(self.group_segments[:-1], self.group_segments[1:])

    def print_debug(self):
        print("labels:    ",self.labels)
        print("parents: ",self.parents)
        print("is_leaf:   ",self.is_leaf)
        print("group_segments: ", self.group_segments)
        print("child_group: ", self.child_group)
        print("encoder: ",self.encoder)
    def decode(self, cat):
        return self.labels[cat]
    def encode(self, label):
        return self.encoder[label]
    
    def get_hierarchy_probability(self, predictions, cat, prob=1.0):
        assert(len(predictions)==self.nlabels)
        parent = self.parents[cat]
        prob = predictions[cat] * prob 
        if parent<0:
            return prob
        return self.get_hierarchy_probability(predictions, parent, prob)
        
    def hierarchy_predictions(self, _predictions, only_leaves=False):
        assert(len(_predictions)==self.nlabels)
        predictions = _predictions.copy()
        
        for idx, p in enumerate(predictions):
            parent = self.parents[idx]
            if parent<0:
                continue
            p_parent = predictions[parent]
            predictions[idx] = p * p_parent
            
        if only_leaves:
            predictions[not self.is_leaf] = 0.0
        return predictions
        
    
    # trace the maximum prediction node in brothers at each depth level.
    def hierarchy_top_prediction(self, predictions, thresh):
        assert(len(predictions)==self.nlabels)
        prob = 1.0
        gidx = 0
        max_idx = -1
        while gidx != None:
            g_range = self.group_segments[gidx:gidx+2]
            g_max_idx = np.argmax(predictions[g_range[0]:g_range[1]]) +g_range[0]
            
            if(prob*predictions[g_max_idx] < thresh):
                # no children satisfies prob > thresh
                if max_idx == -1:
                    return None, 0.0
                return max_idx, prob
            
            prob *= predictions[g_max_idx]
            max_idx = g_max_idx
            gidx = self.child_group[max_idx]
        
        return max_idx, prob
            
        
    @staticmethod
    def rlencode(x, dropna=False):
        """
        Run length encoding.
        Based on http://stackoverflow.com/a/32681075, which is based on the rle 
        function from R.

        Parameters
        ----------
        x : 1D array_like
            Input array to encode
        dropna: bool, optional
            Drop all runs of NaNs.

        Returns
        -------
        start positions, run lengths, run values

        """
        where = np.flatnonzero
        x = np.asarray(x)
        n = len(x)
        if n == 0:
            return (np.array([], dtype=int), 
                    np.array([], dtype=int), 
                    np.array([], dtype=x.dtype))

        starts = np.r_[0, where(~np.isclose(x[1:], x[:-1], equal_nan=True)) + 1]
        lengths = np.diff(np.r_[starts, n])
        values = x[starts]

        if dropna:
            mask = ~np.isnan(values)
            starts, lengths, values = starts[mask], lengths[mask], values[mask]

        return starts, lengths, values
    
    
    @staticmethod
    def serialize_one_depth(tree, parent_label,parent_idx,leaf):
            labels = list(tree.keys())
            if parent_idx >=0 and len(labels)==1:
                # this key has no brother.
                # add parent as the non-'labels[0]' class) 
                labels.append(parent_label)
            labels.sort()
            n_labels = len(labels)
            parents = [parent_idx] * n_labels
            is_leaf = [key not in tree.keys() or tree[key]==None for key in labels]
            subtrees = [None] * n_labels
            for idx, key in enumerate(labels):
                if is_leaf[idx]:
                    continue
                subtrees[idx] = tree[key]
            
            return labels, parents, is_leaf, subtrees
        
    @staticmethod
    def serialize(tree, parent_label=None, leaf=None):
        # return self.labels, self.parents, self.is_leaf    

        arg_stacks = [(tree, parent_label,-1)]

        labels = []
        parents = []
        is_leaf = []
        while len(arg_stacks)>0:
            l=[]
            p=[]
            i=[] 
            subtrees = []
            for args in arg_stacks:
                _l,_p,_i,_s = CategoryTree.serialize_one_depth(*args,leaf)
                l += _l
                p += _p
                i += _i
                subtrees += _s
            arg_stacks = []
            for idx, child in enumerate(l):
                if i[idx]:
                    continue
                arg_stacks.append((subtrees[idx], child, len(labels)+idx))
            labels += l
            parents += p
            is_leaf += i
        return np.array(labels),np.array(parents),np.array(is_leaf)
