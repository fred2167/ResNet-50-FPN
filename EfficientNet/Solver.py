import torch
from torch.utils.tensorboard import SummaryWriter
import matplotlib.pyplot as plt
import random
import pickle
import time
import math


def fixrandomseed(seed=0):
  '''
  Fix random seed to get deterministic outputs
  '''
  torch.manual_seed(seed)
  torch.cuda.manual_seed(seed)
  # random.seed(seed)

def Sampler(model_fn, model_args, train_loader, val_loader, num_model, epoch, lr_lowbound=-5, lr_highbound=0):
  '''
  Use for hyperparameter tuning. Quickly sample learing rates
  Inputs:
    - model_fn: model function
    - model_args: model arguments
    - num_model: number of models for sampling

  '''

  for i in range(num_model):
    model = model_fn(**model_args)
    lr = 10 ** random.uniform(lr_lowbound, lr_highbound)

    print(f'({i+1}/{num_model})lr: {lr:.4e}', end=' ')
    solver = Solver(model, train_loader, val_loader, fp16= False)
    solver.train(epoch, lr)




class Solver(object):
  '''
  Default/ Hard-coded Behavior:
    Using NVDIA GPU, Cuda, Cudnn
    optimizer = Adam
    loss function = Cross Entropy Loss


  '''

  def __init__(self, model, train_loader, val_loader, print_every_iter=200, check_every_epoch=5, fp16= True, tf_board=False, random_seed=0):
    '''
    Inputs:
      - fp16:               if True, both model and data are using torch.float16 to save GPU memory, otherwise using torch.float32.
                            NOTE: when using fp16, Adam optimizer's eps is set to larger than default value to prevent overflow
      - random_seed:        random seed that make output deterministic. See detail in fixrandomseed function
      
    '''
    self.model = model
    self.train_loader = train_loader
    self.val_loader = val_loader
    self.print_every_iter = print_every_iter
    self.check_every_epoch = check_every_epoch
    self.tf_board = tf_board

    # Book keeping variables
    self.stats = {}
    stats_names = ['epoch_loss', 'avg_loss', 'train_acc', 'val_acc','ratio']
    for name in stats_names:
      self.stats[name] = []

    self.stats['print_every_iter'] = print_every_iter
    self.stats['check_every_epoch'] = check_every_epoch

    if tf_board:
      self.writer = SummaryWriter()

    torch.cuda.synchronize()
    torch.backends.cudnn.benchmark = True
    fixrandomseed(random_seed)

    self.to_float_cuda = {"dtype": torch.float16 if fp16 else torch.float32, "device":"cuda"}
    self.eps = 1e-5 if fp16 else 1e-8

    self.model.to(**self.to_float_cuda) # model need to be moved before constructing optimizer
    
    self.optimizer = torch.optim.SGD(model.parameters(), lr=1e-3, momentum=0.9,nesterov=True) 
    self.loss_fn = torch.nn.CrossEntropyLoss()
  

  def train(self, epoch, lr=1e-4 , verbose=False, checkpoint_name= None):
    '''
    Inputs:
      - print_every_iter:   print *loss* and *accumilated time* every number of iters, if verbose is also true
      - check_every_epoch:  checkpoint for every number of epoch. See details behaviors in the private checkpoint function
      - verbose:            print initial loss for sanity check, detail loss during training, otherwise, only print stats in checkpoint
      - checkpoint_name:    if not None, will save model and optimizer state_dict at checkpoint
    '''
    self.epoch = epoch
    self.lr = lr
    self.verbose = verbose
    self.checkpoint_name = checkpoint_name
    checkpoint_cycle_flag = True

    # can change lr when call train again
    if self.optimizer.param_groups[0]['lr'] != lr:
      for p in self.optimizer.param_groups:
        p['lr'] = lr

    num_batch = len(self.train_loader)
    self.model.train()
    for i in range(1, epoch+1):

      total_loss = 0
      iter_loss_history = []
      Y_pred_all = []
      Y_tr_all = []
      start_time = time.time()
      if checkpoint_cycle_flag:
        checkpoint_cycle_flag = False
        checkpoint_start_time = time.time()


      for j, data in enumerate(self.train_loader):
        
        Xtr, Ytr = data
        Xtr, Ytr = Xtr.to(**self.to_float_cuda), Ytr.cuda()

        # alternative of model.zero_grad(). This way is more efficient
        for param in self.model.parameters():
          param.grad = None
        
        y_pred = self.model(Xtr)
        loss = self.loss_fn(y_pred,Ytr)

        total_loss += loss.item()

        loss.backward()
        self.optimizer.step()

        # print training loss per number of iterations
        if verbose and j % self.print_every_iter == 0:
          print(f"Iteration {j}/{num_batch}, loss = {(total_loss / (j+1)):.5f}, took {(time.time() - start_time):.2f} seconds")
        
        # Iter Book keeping
        Y_pred_all.append(y_pred)
        Y_tr_all.append(Ytr)
        iter_loss_history.append(loss.item())



      avg_loss = total_loss / num_batch

      if verbose:
        print(f"Epoch: {i}/{epoch}, loss = {avg_loss:.5f}, took {time.time() - start_time:.2f} seconds")

      # Epoch Book keeping
      self.stats['epoch_loss'].append(iter_loss_history)
      self.stats['avg_loss'].append(avg_loss)

      if self.tf_board: 
        self.writer.add_scalar('Epoch loss', avg_loss, i)

      
      if i % self.check_every_epoch == 0 or i == epoch:
        # check train accuracy by using saved results during forward pass
        Y_pred_all = torch.argmax(torch.cat(Y_pred_all),dim=1)
        Y_tr_all = torch.cat(Y_tr_all)
        train_accuracy = (Y_pred_all == Y_tr_all).float().mean()

        # check val accuracy
        val_accuracy = self._check_accuracy(self.val_loader)

        # check update ratio
        ratio = self._check_update_ratio()

        checkpoint_cycle_flag = True
        print(f'Epoch: {i}/{epoch}, Loss: {avg_loss:.4f} train acc: {train_accuracy:.4f}, val acc: {val_accuracy:.4f}, update ratio: {ratio:.2e}, took {(time.time()-checkpoint_start_time):.2f} seconds')

        # Checkpoint Book keeping
        self.stats['train_acc'].append(train_accuracy)
        self.stats['val_acc'].append(val_accuracy)
        self.stats['ratio'].append(ratio)
        

        if checkpoint_name is not None:
          self._save_checkpoint(epoch=i)

        if self.tf_board:
          self.writer.add_scalar('train_accuracy', train_accuracy, i)
          self.writer.add_scalar('val_accuracy', val_accuracy, i)
  

  @torch.no_grad()
  def _check_update_ratio(self):
    '''
    Check the ratio between weights and its graidents. NOT tracking bias
    Ideal ratio is around 1e-3
    '''
    param_norms = 0
    update_norms = 0
    for param in self.model.parameters():
        param_norms += torch.linalg.norm(param)
        update_norms += torch.linalg.norm(param.grad)

    return (self.lr * update_norms / param_norms).item()

  @torch.no_grad()
  def _check_accuracy(self, data_loader, num_sample=None):
    '''
    if num_sample is provided, will sub sample data loader to the designated amount of samples and double the batch size for efficency.
    (Can double the batch size during evaluation since no grads are needed)

    Inputs:
      - data_loader:    Pytorch dataloader object
      - num_sample:     number of samples that are *randomly* picked in the dataloader
    Outputs:
      - acc:            float, accuracy of the dataloader
    
    TODO:
    - Want randomly pick data to calculate accuracy? is making it more deterministic better?
    '''
    self.model.eval()

    if num_sample is not None:
      sub_dataset = torch.utils.data.Subset(data_loader.dataset, torch.randint(high=len(data_loader.dataset),size=(num_sample,)))
      sub_data_loader = torch.utils.data.DataLoader(sub_dataset,data_loader.batch_size*2, num_workers=2,pin_memory=True,)
    else:
      sub_data_loader = data_loader

    Ypred = []
    Yall = []
    for data in sub_data_loader:
      X, Y = data
      X, Y = X.to(**self.to_float_cuda), Y.cuda()

      scores = self.model(X)
      Ypred.append(torch.argmax(scores,dim=1))
      Yall.append(Y)
    
    Ypred = torch.cat(Ypred)
    Yall = torch.cat(Yall)
    acc = (Ypred == Yall).float().mean()

    self.model.train()
    return acc.item()

 
  def _save_checkpoint(self, epoch):

    checkpoint = {
      'model_state_dict': self.model.state_dict(),
      'optimizer_state_dict': self.optimizer.state_dict(),
      'stats':self.stats
    }
 
    PATH = f'{self.checkpoint_name}_epoch_{epoch}.tar'

    if self.verbose:
      print(f'Saving checkpoint to "{PATH}"')

    torch.save(checkpoint, PATH)

  @staticmethod
  def load_check_point(PATH, model_fn, model_args, train_loader, val_loader):
    checkpoint = torch.load(PATH)
    model = model_fn(**model_args)
    model.load_state_dict(checkpoint['model_state_dict'])
    solver = Solver(model, train_loader, val_loader)
    solver.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    solver.stats = checkpoint['stats']
    solver.check_every_epoch = checkpoint['stats']['check_every_epoch']
    solver.print_every_iter = checkpoint['stats']['print_every_iter']
      
    return solver

  def plot(self):
    plt.figure(figsize=(20,5))
    plt.subplot(131)
    plt.plot(self.stats['epoch_loss'],'oc')
    plt.plot(self.stats['avg_loss'],'-b')
    plt.xlabel('Epoch')
    plt.ylabel('Loss')

    plt.subplot(132)
    plt.plot(self.stats['train_acc'],'-o', label='train')
    plt.plot(self.stats['val_acc'],'-o', label='validation')
    plt.xlabel(f'Every {self.check_every_epoch} Epoch')
    plt.ylabel('Accuracy')
    plt.legend()

    plt.subplot(133)
    plt.plot(self.stats['ratio'],'o')
    plt.yscale('log')
    plt.yticks([1e-2,1e-3,1e-4,1e-5,1e-6],['1e-2','1e-3','1e-4','1e-5','1e-6'])
    plt.xlabel(f'Every {self.check_every_epoch} Epoch')
    plt.ylabel('Update Ratios')
    plt.show()
