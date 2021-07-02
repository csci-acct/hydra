
import torch.nn as nn
from .utilities import get_free_space
import numpy as np
import gc
from timeit import default_timer as timer

import torch
from .Container import NNContainer
import traceback



class ShardedTask():

    def __init__(self, model, direction, time_taken, idx):
        
        self.model = model
        self.direction = direction
        self.time_cost = time_taken
        self.idx = idx
        self.optimizer = torch.optim.SGD(self.model.parameters(), lr = 0.01)



class Model():
    def __init__(self, model):
        self.f_shards = []
        self.b_shards = []
        # this info is nice to have for the scheduler
        self.shard_forward_times = []
        self.shard_backward_times = []
        self.shard_times = []
        self.total_time = 0

        self.verbose = 0

        self.layers = list(model.children()) # ideally we'd use model.modules. But that could lead to
                                             # issues in our sharding algo. TODO

    def setup_manual(self, true_criterion, batch_orig, indices):
        shard_idx = 0
        true_labels = batch_orig[-1]
        batch_orig = batch_orig[0:len(batch_orig)-1]
        device = torch.device("cuda:0")
        batch = [x.to(device) for x in batch_orig]
        
        for shard_starts in range(len(indices) - 1):
            list_of_layers = self.layers[indices[shard_starts]:indices[shard_starts+1]]
            print("Layers {} to {}".format(indices[shard_starts], indices[shard_starts+1]))
            start_f = timer()
            model = NNContainer(list_of_layers)
            if (isinstance(batch, list) or isinstance(batch, tuple)):
                batch = [x.to(device) for x in batch]
            else:
                batch = batch.to(device)
            model.to(device)
            out = model(batch)
            end_f = timer()
            start_b = timer()
            labels = out.detach().clone()
            criterion = nn.MSELoss()
            loss = criterion(out, labels)
            print("loss")
            if loss.requires_grad:
                loss.backward()
                model.zero_grad()
            model.zero_grad()
            del loss
            del criterion
            del labels
            
            if (isinstance(batch, list) or isinstance(batch, tuple)):
                batch = [x.cpu() for x in batch]
            del batch
            batch = out.cpu().detach().clone()
            del out
            

            model.to(torch.device("cpu"))  # this is an inplace operation
            end_b = timer()
            
            
            self.f_shards.append(ShardedTask(model, "f", end_f - start_f, shard_idx))
            self.b_shards.append(ShardedTask(model, "b", end_b - start_b, shard_idx))
            self.total_time = self.total_time + (end_f - start_f) + (end_b - start_b)
            shard_idx+=1
            
        print("Layers {} to {}".format(indices[-1], len(self.layers)-1))
        list_of_layers = self.layers[indices[-1]:]
        start_f = timer()
        model = NNContainer(list_of_layers)
        if (isinstance(batch, list) or isinstance(batch, tuple)):
            batch = [x.to(device) for x in batch]
        else:
            batch = batch.to(device)
        model.to(device)
        out = model(batch)
        end_f = timer()
        start_b = timer()
        
        criterion = nn.MSELoss()
        
        if not isinstance(true_labels, torch.Tensor):
            true_labels = [x.to(device, non_blocking=True) for x in true_labels]
        else:
            true_labels = true_labels.to(device, non_blocking=True)
        loss = true_criterion(out, true_labels)
        loss.backward()
        model.zero_grad()
        del loss
        del criterion
        del true_labels
        del out
        if (isinstance(batch, list) or isinstance(batch, tuple)):
            batch = [x.cpu() for x in batch]
        del batch


        model.to(torch.device("cpu"))  # this is an inplace operation
        end_b = timer()


        self.f_shards.append(ShardedTask(model, "f", end_f - start_f, shard_idx))
        self.b_shards.append(ShardedTask(model, "b", end_b - start_b, shard_idx))
        self.total_time = self.total_time + (end_f - start_f) + (end_b - start_b)

        
        self.b_shards.reverse()
        self.b_shards.pop(0)
        
    
    
    def setup(self, true_criterion, batch_orig, buffer):
        
        available_gpus = torch.cuda.device_count()
        available_devices = list(range(available_gpus))
        free_spaces = [get_free_space(x) for x in available_devices]
        
        device_idx = np.argmin(free_spaces)
        device = torch.device("cuda:"+str(device_idx))
        if (self.verbose == 1):
            print(free_spaces)
            print("Experimental sharding will occur on device {}.".format(device_idx))
    
        if (buffer != None):
            buffer_arr = torch.zeros((buffer, buffer)).to(device)
        else:
            buffer_arr = torch.zeros((18000, 18000)).to(device)
            print("Buffer Arr created.")
            print([get_free_space(x) for x in available_devices])
            
        true_labels = batch_orig[-1]
        batch_orig = batch_orig[0:len(batch_orig)-1]
        
        batch = [x.to(device) for x in batch_orig]
        
        print("Batch Transferred.")
        print([get_free_space(x) for x in available_devices])

        
        list_of_layers = nn.ModuleList()

        true_layer_index = 0
        shard_idx = 0
        #print("Free memory: " + str(get_free_space()))
        while true_layer_index < (len(self.layers)):
            oom = False
            oom_override = False
            #if (self.verbose == 1):
            #    print("Current Memory Free {0} MB\t".format(get_free_space(device_idx)/(1024*1024)))


            # TRY ADDING LAYER
            try:    
                list_of_layers.append(self.layers[true_layer_index])
                model = NNContainer(list_of_layers)
                model.to(device)
                true_layer_index+=1
                
                mem_params = sum([param.nelement()*param.element_size() for param in model.parameters()])
                mem_bufs = sum([buf.nelement()*buf.element_size() for buf in model.buffers()])
                mem = mem_params + mem_bufs
                
                if (mem > 1073741824):
                    print("Model size: {}".format(mem))
                    oom_override = True # ALL devices need to have AT LEAST 1GB available for model caching!
                    raise Exception()
                    
                #print("Layer created.")
                print([get_free_space(x) for x in available_devices])
            except Exception as e:
                if (self.verbose == 1):
                    print(e)
                oom = True
            if (oom):
                print("Split at layer {}".format(true_layer_index))
                for p in model.parameters():
                    if p.grad is not None:
                        del p.grad  # free some memory
                #model.layers = None
                del model
                del list_of_layers[-1]
                torch.cuda.empty_cache()

                if (len(list_of_layers) == 0):
                    raise RuntimeError("Your minimum defined module's size is too large! Try chunking your modules into smaller pieces?")

                model = NNContainer(list_of_layers)
                model.to(device)
                oom = False
                
            # TRY A FORWARD PASS
            try:
                out = model(batch)
                if not (isinstance(out, torch.Tensor)):
                    grads = []
                    new_out = []
                    for output in out:
                        if output.requires_grad:
                            grads.append(torch.ones_like(output).to(device))
                            new_out.append(output)
                    if (len(new_out)!= 0):
                        torch.autograd.backward(new_out, grads)

                else:
                    labels = out.detach().clone()
                    criterion = nn.MSELoss()
                    loss = criterion(out, labels)
                    if loss.requires_grad:
                        loss.backward()
                    model.zero_grad()

                    del loss
                    del criterion
                    del labels

                #print("Pass run.")
                print([get_free_space(x) for x in available_devices])
                

                del out

                for p in model.parameters():
                    if p.grad is not None:
                        del p.grad  # free some memory
                gc.collect()
                torch.cuda.empty_cache()
                #print("Pass cleanup.")
                #print([get_free_space(x) for x in available_devices])        
                
                #model.layers = None
                model.cpu()
                #del model
                gc.collect()
                torch.cuda.empty_cache()
                
                #print("Model cleanup.")
                #print([get_free_space(x) for x in available_devices])
                    
            # IF NEEDED, ROLLBACK A PASS
            except Exception as e:
                print(e)
                print(traceback.format_exc())
                oom = True
            if oom or oom_override:
                print("Split at layer {}".format(true_layer_index))
                #print("Current Memory Free in FAIL {0} MB\t".format(get_free_space(device_idx)/(1024*1024)))

                for p in model.parameters():
                    if p.grad is not None:
                        del p.grad  # free some memory
                model.cpu()
                #del model.layers
                del model
                del list_of_layers[-1]
                if (len(list_of_layers) == 0):
                    
                    raise RuntimeError("Exit")
                try:
                    del out
                except:
                    pass
                try:
                    del loss
                except:
                    pass
                try:
                    del labels
                except:
                    pass
                try:
                    del criterion
                except:
                    pass
                gc.collect()
                torch.cuda.empty_cache()


                start_f = timer() # used for scheduler

                model = NNContainer(list_of_layers)
                model.to(device)

                    
                out = model(batch)
                end_f = timer()

                start_b = timer() # used for scheduler
                
                labels = out.detach().clone()
                criterion = nn.MSELoss()
                loss = criterion(out, labels)
                loss.backward()
                model.zero_grad()

                
                del loss
                del criterion
                del labels

                for p in model.parameters():
                    if p.grad is not None:
                        del p.grad  # free some memory
                model.cpu()  # this is an inplace operation

                gc.collect()
                torch.cuda.empty_cache()

                end_b = timer()

                #model.share_memory()
                self.f_shards.append(ShardedTask(model, "f", end_f-start_f, shard_idx))
                self.b_shards.append(ShardedTask(model, "b", end_b-start_b, shard_idx))
                shard_idx+=1

                self.total_time = self.total_time + (end_f - start_f) + (end_b - start_b)
                


                list_of_layers = nn.ModuleList()
        
                if (isinstance(batch, list) or isinstance(batch, tuple)):
                    batch = [x.cpu() for x in batch]  
                del batch
                batch = out.cpu().detach().clone()
                del out
                gc.collect()
                torch.cuda.empty_cache()
                #print()
                #print("Split point found! After handling, {0} MB Free.".format(get_free_space() / (1024 * 1024)), end='\r')
                #print()
                batch = batch.to(device)
                #print([get_free_space(x) for x in available_devices])


        if (len(list_of_layers) > 0):
            
            start_f = timer() # used for scheduler
            
            model = NNContainer(list_of_layers)
            model.to(device)
            out = model(batch)
            
            end_f = timer()
            self.shard_forward_times.append(end_f-start_f)

            
            start_b = timer() # used for scheduler
            if not isinstance(true_labels, torch.Tensor):
                true_labels = [x.to(device, non_blocking=True) for x in true_labels]
            else:
                true_labels = true_labels.to(device, non_blocking=True)

            loss = true_criterion(out, true_labels)
            loss.backward()
            model.zero_grad()
            del loss
            del true_criterion
            del true_labels
            del out
            if (isinstance(batch, list) or isinstance(batch, tuple)):
                batch = [x.cpu() for x in batch]
            del batch

            model.to(torch.device("cpu"))  # this is an inplace operation

            end_b = timer()
            self.f_shards.append(ShardedTask(model, "f", end_f - start_f, shard_idx))
            self.b_shards.append(ShardedTask(model, "b", end_b - start_b, shard_idx))

            self.total_time = self.total_time + (end_f - start_f) + (end_b - start_b)

            gc.collect()
            torch.cuda.empty_cache()
            
            self.b_shards.reverse()
            self.b_shards.pop(0)
        
        if (buffer_arr != None):
            print("Clearing out the buffer array.")
            del buffer_arr
            torch.cuda.empty_cache()


