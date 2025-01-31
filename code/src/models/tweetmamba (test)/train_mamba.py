import torch
import torch.nn as nn

import sys
import os

sys.path.append(os.path.dirname(__file__))
from mamba.mamba_encoder import MambaConfig, MambaEncoder

from torch.utils.data import Dataset
from transformers import AutoModel, AutoTokenizer
import torch
import torch.nn as nn
import torch.nn.functional as F
from pathlib import Path
import pandas as pd
import re
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from tqdm import tqdm
import gensim.downloader as api
import numpy as np
import torch.nn.utils.rnn as rnn_utils
from torch.utils.data import DataLoader
from transformers import Trainer, TrainingArguments



name = 'test'
bidirectional = True
depth = 1

log_dir = "./logs/model_" + ("bimamba" if bidirectional else "mamba")  + name + "_depth_" + str(depth)
results_dir = "./results/model_" + ("bimamba" if bidirectional else "mamba")  + name+"_depth_" + str(depth)

MAX_TWEETS_PER_GROUP = 2000

max_n_tweets = MAX_TWEETS_PER_GROUP  # Nombre maximum de tweets par groupe
max_n_words = 20   # Nombre maximum de mots par tweet
embedding_dim = 200  # Dimension des embeddings
num_classes = 2  # Nombre de classes pour les labels

debug = False # Si True, génère des données aléatoires pour le débogage


# Load and combine all CSV files from the directory
if not debug and __name__ == "__main__":
    print("model loading...")
    embeddings_model = api.load("glove-twitter-200")  # 200-dimensional GloVe embeddings
    print("model loaded.")
    print("Loading data...")
    data_dir = Path("./challenge_data/train_tweets")
    data_files = list(data_dir.glob("*.csv"))

    if not data_files:
        raise FileNotFoundError(f"No CSV files found in {data_dir}")

    dataframes = [pd.read_csv(file) for file in data_files]
    df = pd.concat(dataframes, ignore_index=True)
    print(f"Loaded {len(df)} rows from {len(data_files)} files.")

    # Preprocess tweets
    def preprocess_text(text):
        """Clean and preprocess text data."""
        text = text.lower()
        text = re.sub(r"#\w+", "", text)      # Remove hashtags
        text = re.sub(r"http\S+", "", text)   # Remove URLs
        text = re.sub(r"@\w+", "", text)      # Remove mentions
        text = re.sub(r"\brt\b", "", text)    # Remove retweet indicator
        text = re.sub(r"[^a-zA-Z\s]", "", text)  # Remove non-alphabetic characters
        text = re.sub(r"\s+", " ", text)      # Remove multiple spaces
        text = text.strip()                   # Remove leading/trailing spaces
        tokens = text.split()   
        # Calcul de l'embedding
        embeddings = []
        for token in tokens:
            if token in embeddings_model:
                embeddings.append(embeddings_model[token])
        if embeddings == []:
            embeddings = [np.zeros(embedding_dim)]
        return embeddings  # Retourne l'embedding du tweet              # Tokenize by splitting on spaces

    print("Preprocessing tweets...")
    tqdm.pandas()
    df["CleanTweet"] = df["Tweet"].progress_apply(preprocess_text)

    print("Preprocessing complete.")

    # Aggregate tweets by ID and EventType

    tweets = df.groupby(["ID", "EventType"], as_index=False).agg({"CleanTweet": lambda x: list(x)[:MAX_TWEETS_PER_GROUP]})

    

    train_texts, eval_texts, train_labels, eval_labels = train_test_split(
        tweets["CleanTweet"].tolist(),
        tweets["EventType"].tolist(),
        test_size=0.2,
        random_state=42
    )

if debug: # Génération de données aléatoires pour le débogage
    num_samples = 100  # Nombre de groupes de tweets
    texts = [
        [
            np.random.rand(np.random.randint(1, max_n_words + 1), embedding_dim).tolist()
            for _ in range(np.random.randint(1, max_n_tweets + 1))
        ]
        for _ in range(num_samples)
    ]
    labels = [np.random.randint(0, num_classes) for _ in range(num_samples)]
    train_texts, eval_texts, train_labels, eval_labels = train_test_split(
            texts,
            labels,
            test_size=0.2,
            random_state=42
        )

class PrecomputedEmbeddingDataset(Dataset):
    def __init__(self, tweets_embeddings_list, labels):
        self.tweets_embeddings_list = tweets_embeddings_list
        self.labels = labels

    def __len__(self):
        return len(self.tweets_embeddings_list)

    def __getitem__(self, idx):
        tweets_embeddings = self.tweets_embeddings_list[idx]
        label = self.labels[idx]
        tweets_embeddings = [torch.tensor(embedding, dtype=torch.float) for embedding in tweets_embeddings]
        result = {
            'input_ids': tweets_embeddings,
            'n_tweets': torch.tensor(len(tweets_embeddings), dtype=torch.long),
            'n_words': torch.tensor([te.size(0) for te in tweets_embeddings], dtype=torch.long),
            'labels': torch.tensor(label, dtype=torch.float)
        }
        return result

if __name__ == "__main__":
    train_dataset = PrecomputedEmbeddingDataset(train_texts, train_labels)
    eval_dataset = PrecomputedEmbeddingDataset(eval_texts, eval_labels)



    
class TweetMamba(nn.Module):
    def __init__(self, depth=depth, embed_dim=embedding_dim ,bidirectional = bidirectional):
        super().__init__()
        
        #self.pos_embed =  PosEmbed(num_patches = self.num_patches,embed_dim=embed_dim)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        # Attention pondérée pour les mots
        self.word_attention = nn.Linear(embedding_dim, 1)

        
        config  = MambaConfig(d_model = embed_dim, n_layers= depth,bidirectional=bidirectional)
        self.mamba = MambaEncoder(config)
        self.head = nn.Linear(embed_dim, 1)
        self.criterion = nn.BCEWithLogitsLoss()


    def attention_aggregation(self, word_embeddings):
        """
        Applique une attention pondérée sur les embeddings des mots d'un tweet.

        Arguments:
        - word_embeddings: Tensor de dimensions (n_words, embedding_dim)

        Retourne:
        - tweet_embedding: Tensor de dimensions (embedding_dim)
        """
        attn_scores = self.word_attention(word_embeddings)  # (n_words, 1)
        attn_scores = F.softmax(attn_scores, dim=0)
        tweet_embedding = torch.sum(attn_scores * word_embeddings, dim=0)
        return tweet_embedding


    def forward(self, input_ids=None,  n_tweets=None, n_words=None,labels = None):
        batch_size = input_ids.size(0)
        max_n_tweets = input_ids.size(1)  # Number of tweets per batch (already padded)
        embedding_dim = input_ids.size(-1)  # Embedding dimension

        # Initialize tensor for storing tweet embeddings
        x = torch.zeros(batch_size, max_n_tweets, embedding_dim, device=input_ids.device)

        for i in range(batch_size):
            tweets = input_ids[i]  # (max_n_tweets, max_n_words, embedding_dim)

            for j in range(n_tweets[i]):
                word_embeddings = tweets[j][:n_words[i][j]]  # Extract the actual words in the tweet
                # Apply attention on the words of the tweet
                tweet_embedding = self.attention_aggregation(word_embeddings)  # (embedding_dim,)
                x[i, j] = tweet_embedding  # Assign to the correct location in the tensor

      
        cls_token = self.cls_token.expand(batch_size, -1, -1)
        token_position = max_n_tweets // 2
        x = torch.cat((x[:, :token_position, :], cls_token, x[:, token_position:, :]), dim=1)
        x = self.mamba(x) #Mamba Encoder : (B, N+1, embed_dim) -> (B, N+1, embed_dim) 

        #  We keep only CLS token at position N//2
        x = x[:, token_position, :]
        x = self.head(x).squeeze(1)
        
        probs = torch.sigmoid(x)


        outputs = {'logits': probs}

        if labels is not None:
            loss = self.criterion(x, labels)
            outputs['loss'] = loss

        return outputs

    

def collate_fn(batch,max_n_tweets=max_n_tweets, max_n_words=max_n_words,embedding_dim=embedding_dim):
    batch_size = len(batch)
    input_ids = torch.zeros((batch_size, max_n_tweets, max_n_words,embedding_dim), dtype=torch.float)
    n_tweets = torch.zeros((batch_size,), dtype=torch.long)
    n_words = torch.zeros((batch_size, max_n_tweets), dtype=torch.long)

    for i,sample in enumerate(batch):
        n_tweets[i] = min(sample['n_tweets'].item(), max_n_tweets)
        for j,tweet in enumerate(sample['input_ids'][:n_tweets[i]]):
            n_words[i,j] = min(sample['n_words'][j].item(), max_n_words)

            input_ids[i, j, :n_words[i, j],:] = tweet[:n_words[i, j]]


    
    return {
        'input_ids': input_ids,  # List of lists of Tensors
        'n_tweets': n_tweets,  # Tensor of tweet counts
        'n_words': n_words,  # Tensor of word counts
        'labels':  torch.stack([item['labels'] for item in batch])           # Tensor of labels
    }


# Initialisation du modèle

criterion = nn.BCELoss()

# Entraînement
batch_size = 4

training_args = TrainingArguments(
    output_dir=results_dir,
    per_device_train_batch_size=batch_size,
    per_device_eval_batch_size=batch_size,
    eval_strategy='epoch',
    logging_dir=log_dir,
    logging_steps=10,
    load_best_model_at_end=True,

    save_strategy='epoch',

    num_train_epochs=20,              # Nombre d'époques
    weight_decay=0.01,               # Régularisation L2
    learning_rate=5e-4,      # Taux d'apprentissage
    gradient_accumulation_steps=4,  # Accumulation de gradients
    save_total_limit=2,   

               
    metric_for_best_model="eval_accuracy",  # Use eval_accuracy to determine the best model
    greater_is_better=True,           # Higher accuracy is better
)



def compute_metrics(eval_pred):
    probs, labels = eval_pred
    # Appliquer la sigmoïde pour obtenir des probabilités
    probs = torch.tensor(probs)
    preds = (probs >= 0.5).float().numpy()
    labels = labels
    precision, recall, f1, _ = precision_recall_fscore_support(labels, preds, average='binary')
    acc = accuracy_score(labels, preds)
    return {
        'accuracy': acc,
        'f1': f1,
        'precision': precision,
        'recall': recall
    }


if __name__ == "__main__":
    model = TweetMamba(embed_dim=embedding_dim,bidirectional=bidirectional)
    trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    data_collator=collate_fn,
    compute_metrics=compute_metrics
    )

    trainer.train()
    print("Training complete.")
    trainer.save_model(results_dir+'/bestmodel')
    print("Model saved.")
